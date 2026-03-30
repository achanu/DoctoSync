#!/usr/bin/env python
"""Script de synchronisation Doctolib -> Google Calendar."""

import argparse
import datetime
from datetime import timedelta
import os
import sys
from typing import Any, Optional

import requests

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from docto_common import (
    _ANSI_BLUE,
    _ANSI_GREEN,
    _ANSI_RED,
    _ANSI_RESET,
    _CACHE_FILE_DEFAULT,
    fetch_doctolib,
    get_cookies,
    load_cache,
    load_yaml,
    save_cache,
)

SCOPES = ['https://www.googleapis.com/auth/calendar']

_TIMEZONE = 'Europe/Paris'

def get_calendar_service(config: dict[str, Any]) -> Any:
    """Authentifie l'utilisateur et retourne le service Google Calendar.

    Args:
        config: Configuration contenant les chemins vers les credentials.

    Returns:
        Un objet service Google Calendar authentifié.
    """
    creds = None
    token_path = config['calendar'].get('token_path', 'config/token.json')

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config['calendar']['credentials_path'], SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(token_path, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())

    return build('calendar', 'v3', credentials=creds)


def fetch_google_events(
        service: Any,
        calendar_id: str,
        start_date: str,
) -> dict[str, Any]:
    """Récupère les événements Google Calendar existants pour une semaine.

    Args:
        service: Service Google Calendar authentifié.
        calendar_id: Identifiant du calendrier Google.
        start_date: Date de début de la semaine au format 'YYYY-MM-DD'.

    Returns:
        Dictionnaire {SYNC_KEY: événement} pour les événements synchronisés.
    """
    start_dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
    t_min = start_dt.replace(hour=0, minute=0, second=0).isoformat() + 'Z'
    t_max = (
        (start_dt + timedelta(days=7))
        .replace(hour=0, minute=0, second=0)
        .isoformat() + 'Z'
    )

    events = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=t_min,
            timeMax=t_max,
            singleEvents=True,
        )
        .execute()
        .get('items', [])
    )

    mapping = {}
    for event in events:
        desc = event.get('description', '')
        if 'SYNC_KEY:' in desc:
            mapping[desc.split('SYNC_KEY:')[1].strip()] = event
    return mapping


def _create_event_body(
        rdv: dict[str, Any],
        key: str,
        notifs: tuple[int, int],
        last_day: Optional[str],
        loc: str,
) -> tuple[dict[str, Any], str]:
    """Crée le corps d'un événement Google Calendar.

    Args:
        rdv: Rendez-vous Doctolib nettoyé.
        key: Clé unique de synchronisation (SYNC_KEY).
        notifs: Tuple (notif_std, notif_first) — délais de notification en
            minutes (standard, premier RDV du jour).
        last_day: Date du dernier RDV traité ('YYYY-MM-DD') ou None.
        loc: Adresse du lieu (peut être vide).

    Returns:
        Un tuple (corps de l'événement, date du RDV courant au format
        'YYYY-MM-DD').
    """
    notif_std, notif_first = notifs
    day = rdv['start'].split('T')[0]
    mins = notif_first if (day != last_day and notif_first > 0) else notif_std

    body: dict[str, Any] = {
        'summary': f"{rdv['summary']} [{rdv['status']}]",
        'description': f'Synchronisé depuis Doctolib. SYNC_KEY: {key}',
        'start': {'dateTime': rdv['start'], 'timeZone': _TIMEZONE},
        'end': {'dateTime': rdv['end'], 'timeZone': _TIMEZONE},
        'reminders': (
            {
                'useDefault': False,
                'overrides': [{'method': 'popup', 'minutes': mins}],
            }
            if mins > 0
            else {'useDefault': True}
        ),
    }
    if loc:
        body['location'] = loc

    return body, day


def _print_sync_stats(
        week_date: str,
        total_rdvs: int,
        stats: dict[str, int],
        delete_count: int,
) -> None:
    """Affiche le résumé coloré de la synchronisation pour une semaine.

    Args:
        week_date: Date de début de la semaine au format 'YYYY-MM-DD'.
        total_rdvs: Nombre total de RDV Doctolib pour la semaine.
        stats: Dictionnaire {'add': n, 'upd': n} des opérations effectuées.
        delete_count: Nombre d'événements supprimés du calendrier.
    """
    total_str = (
        f'{_ANSI_BLUE}{total_rdvs}{_ANSI_RESET}'
        if total_rdvs > 0 else str(total_rdvs)
    )
    added_str = (
        f'{_ANSI_GREEN}+{stats["add"]} créés{_ANSI_RESET}'
        if stats['add'] > 0 else f'+{stats["add"]} créés'
    )
    updated_str = (
        f'{_ANSI_BLUE}~{stats["upd"]} maj{_ANSI_RESET}'
        if stats['upd'] > 0 else f'~{stats["upd"]} maj'
    )
    deleted_str = (
        f'{_ANSI_RED}-{delete_count} supprimés{_ANSI_RESET}'
        if delete_count > 0 else f'-{delete_count} supprimés'
    )

    print(
        f'Semaine du {week_date} : {total_str} RDVs | '
        f'{added_str} / {updated_str} / {deleted_str}'
    )


def sync_week(  # pylint: disable=too-many-locals
        service: Any,
        config: dict[str, Any],
        rdvs: list[dict[str, Any]],
        existing_map: dict[str, Any],
        week_date: str,
) -> None:
    """Synchronise une semaine de RDV Doctolib vers Google Calendar.

    Crée, met à jour ou supprime les événements selon les différences
    détectées entre Doctolib et Google Calendar.

    Args:
        service: Service Google Calendar authentifié.
        config: Configuration globale du script.
        rdvs: Liste des RDV Doctolib pour la semaine.
        existing_map: Événements Google existants indexés par SYNC_KEY.
        week_date: Date de début de la semaine au format 'YYYY-MM-DD'.
    """
    notif_std = config['config'].get('notification', 30)
    notif_first = config['config'].get('first_of_day', 0)
    loc = config['config'].get('localisation', '').strip()
    calendar_id = config['calendar']['id']

    rdvs.sort(key=lambda x: x['start'])

    last_day = None
    to_delete = set(existing_map.keys())
    stats = {'add': 0, 'upd': 0}

    for rdv in rdvs:
        key = f"{rdv['start']}|{rdv['end']}|{rdv['new_patient']}"
        body, last_day = _create_event_body(
            rdv, key, (notif_std, notif_first), last_day, loc
        )

        if key in existing_map:
            to_delete.discard(key)
            event = existing_map[key]
            if (event.get('location', '').strip() != loc
                    or event.get('reminders') != body['reminders']):
                service.events().update(
                    calendarId=calendar_id,
                    eventId=event['id'],
                    body=body,
                ).execute()
                stats['upd'] += 1
        else:
            service.events().insert(
                calendarId=calendar_id, body=body
            ).execute()
            stats['add'] += 1

    for k in to_delete:
        service.events().delete(
            calendarId=calendar_id,
            eventId=existing_map[k]['id'],
        ).execute()

    _print_sync_stats(week_date, len(rdvs), stats, len(to_delete))


def main() -> None:
    """Point d'entrée principal du script de synchronisation."""
    parser = argparse.ArgumentParser()
    parser.add_argument('-w', '--weeks', type=int, default=1)
    parser.add_argument(
        '--cache-file',
        default=_CACHE_FILE_DEFAULT,
        help='Chemin du fichier de cache partagé avec docto_heatmap.',
    )
    parser.add_argument(
        '--no-cache',
        action='store_true',
        help='Ne pas écrire dans le cache après la synchro.',
    )
    args = parser.parse_args()

    config = load_yaml('config/config.yaml')
    cache_path = None if args.no_cache else args.cache_file

    cookies = get_cookies(config['api']['cookie_path'])
    if not cookies:
        print(
            f'{_ANSI_RED}Attention: Aucun cookie chargé '
            f'(ni navigateur, ni fichier).{_ANSI_RESET}'
        )

    try:
        service = get_calendar_service(config)
    except (ValueError, OSError, HttpError) as e:
        sys.exit(f'Erreur Auth Google: {e}')

    monday = (
        datetime.date.today()
        - timedelta(days=datetime.date.today().weekday())
    )

    print(f'Synchronisation sur {args.weeks} semaine(s)...')

    cache: dict[str, list] = load_cache(cache_path) if cache_path else {}
    cache_updated = False

    for i in range(args.weeks):
        w_start = (monday + timedelta(weeks=i)).strftime('%Y-%m-%d')

        try:
            all_rdvs = fetch_doctolib(config, w_start, cookies)

            # Alimente le cache avec les données brutes (annulés inclus).
            if cache_path:
                cache[w_start] = all_rdvs
                cache_updated = True

            # Filtre les RDVs confirmés pour la synchro Google Calendar.
            sync_rdvs = [
                {**r, 'summary': 'Nouveau patient' if r['new_patient'] else 'Suivi'}
                for r in all_rdvs if not r['cancelled']
            ]

            existing = fetch_google_events(
                service, config['calendar']['id'], w_start
            )
            sync_week(service, config, sync_rdvs, existing, w_start)
        except requests.RequestException as e:
            if i == 0:
                sys.exit(
                    f'Erreur FATALE: Échec de la connexion Doctolib pour la '
                    f'semaine {w_start}. Vérifiez les cookies/URL.\n'
                    f"Détail de l'erreur: {e}"
                )
            print(
                f'Erreur semaine {w_start}: Impossible de récupérer les RDV.'
                f' Passage à la semaine suivante.\nDétail: {e}'
            )
        except HttpError as e:
            print(
                f'Erreur Google Calendar pour la semaine {w_start}: {e}'
            )

    if cache_updated and cache_path:
        save_cache(cache_path, cache)
        print(f'{_ANSI_GREEN}Cache mis à jour : {cache_path}{_ANSI_RESET}')


if __name__ == '__main__':
    main()
