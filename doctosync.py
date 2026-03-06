#!/usr/bin/env python
"""Script de synchronisation Doctolib -> Google Calendar."""

import argparse
import datetime
from datetime import timedelta
import os
import sys
from typing import Any, Optional

import browser_cookie3
import requests
import yaml

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ['https://www.googleapis.com/auth/calendar']

_TIMEZONE = 'Europe/Paris'

_ANSI_GREEN = '\033[92m'
_ANSI_BLUE = '\033[94m'
_ANSI_RED = '\033[91m'
_ANSI_RESET = '\033[0m'


def load_yaml(path: str) -> dict[str, Any]:
    """Charge la configuration depuis un fichier YAML.

    Args:
        path: Chemin vers le fichier de configuration.

    Returns:
        Le contenu du fichier YAML sous forme de dictionnaire.

    Raises:
        SystemExit: Si le fichier est introuvable.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f'Erreur: Fichier introuvable {path}')


def get_cookies(path: str) -> Any:
    """Charge les cookies Doctolib depuis le navigateur ou un fichier.

    Tente d'abord une détection automatique via les navigateurs installés,
    puis se rabat sur le fichier de cookies si nécessaire.

    Args:
        path: Chemin vers le fichier de cookies (format Netscape ou
            clé=valeur).

    Returns:
        Un CookieJar si trouvé dans le navigateur, un dict de cookies si
        chargé depuis le fichier, ou un dict vide si aucun cookie trouvé.
    """
    domain = '.doctolib.fr'
    loaders = [
        browser_cookie3.chrome,
        browser_cookie3.firefox,
        browser_cookie3.edge,
        browser_cookie3.brave,
        browser_cookie3.chromium,
        browser_cookie3.safari,
    ]

    print(
        f'{_ANSI_BLUE}Recherche automatique des cookies '
        f'Doctolib...{_ANSI_RESET}'
    )

    for loader in loaders:
        try:
            cj = loader(domain_name=domain)
            if cj and len(cj) > 0:
                print(
                    f'{_ANSI_GREEN}Cookies trouvés dans le navigateur '
                    f'(via {loader.__name__}).{_ANSI_RESET}'
                )
                return cj
        except Exception:  # noqa: BLE001 — erreurs navigateur ignorées
            continue

    print(
        f'{_ANSI_RED}Aucun cookie trouvé automatiquement. '
        f'Tentative via fichier...{_ANSI_RESET}'
    )

    if not os.path.exists(path):
        return {}

    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    cookies: dict[str, str] = {}
    if '\t' in content:  # Format Netscape (supposé si tabulations présentes).
        for line in content.splitlines():
            parts = line.strip().split('\t')
            if len(parts) >= 7 and not line.startswith('#'):
                cookies[parts[5]] = parts[6]
    else:  # Format simple (clé=val; clé=val).
        for pair in content.replace('; ', ';').split(';'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                cookies[k] = v
    return cookies


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


def fetch_doctolib(
        config: dict[str, Any],
        start_date: str,
        cookies: Any,
) -> list[dict[str, Any]]:
    """Récupère et nettoie les RDV Doctolib pour une semaine donnée.

    Args:
        config: Configuration contenant les paramètres de l'API Doctolib.
        start_date: Date de début de la semaine au format 'YYYY-MM-DD'.
        cookies: Cookies d'authentification Doctolib.

    Returns:
        Liste de RDV nettoyés, chacun sous forme de dictionnaire.

    Raises:
        requests.RequestException: En cas d'erreur réseau ou HTTP.
    """
    api = config['api']
    start_dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = start_dt + timedelta(days=7) - timedelta(seconds=1)

    params = {
        'agenda_ids': api['agenda_ids'],
        'start_date': start_dt.strftime(api['date_format']),
        'end_date': end_dt.strftime(api['date_format']),
        'view': 'week',
        'include_patients': 'true',
    }

    resp = requests.get(
        api['url'],
        params=params,
        cookies=cookies,
        headers={'User-Agent': api.get('user_agent', 'Mozilla/5.0')},
        timeout=10,
    )
    resp.raise_for_status()

    clean_rdvs = []
    for item in resp.json().get('data', []):
        status = item.get('status', 'confirmed').lower()
        if status in ('deleted', 'no_show_but_ok'):
            continue

        is_new = item.get('new_patient', False)
        clean_rdvs.append({
            'start': item.get('start_date'),
            'end': item.get('end_date'),
            'new_patient': is_new,
            'summary': 'Nouveau patient' if is_new else 'Suivi',
            'status': item.get('status', 'confirmed'),
        })
    return [r for r in clean_rdvs if r['start'] and r['end']]


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
        notif_std: int,
        notif_first: int,
        last_day: Optional[str],
        loc: str,
) -> tuple[dict[str, Any], str]:
    """Crée le corps d'un événement Google Calendar.

    Args:
        rdv: Rendez-vous Doctolib nettoyé.
        key: Clé unique de synchronisation (SYNC_KEY).
        notif_std: Délai de notification standard en minutes.
        notif_first: Délai de notification pour le premier RDV du jour,
            en minutes.
        last_day: Date du dernier RDV traité ('YYYY-MM-DD') ou None.
        loc: Adresse du lieu (peut être vide).

    Returns:
        Un tuple (corps de l'événement, date du RDV courant au format
        'YYYY-MM-DD').
    """
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


def sync_week(
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
            rdv, key, notif_std, notif_first, last_day, loc
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
    args = parser.parse_args()

    config = load_yaml('config/config.yaml')

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

    for i in range(args.weeks):
        w_start = (monday + timedelta(weeks=i)).strftime('%Y-%m-%d')

        try:
            rdvs = fetch_doctolib(config, w_start, cookies)
            existing = fetch_google_events(
                service, config['calendar']['id'], w_start
            )
            sync_week(service, config, rdvs, existing, w_start)
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


if __name__ == '__main__':
    main()
