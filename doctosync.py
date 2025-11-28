#!/usr/bin/env python
# pylint: disable=C0301

"""
Script simplifié de synchronisation Doctolib -> Google Calendar.
"""

import argparse
import datetime
from datetime import timedelta
import os
import sys
import yaml
import requests

# Imports Google
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ['https://www.googleapis.com/auth/calendar']

# Couleurs ANSI pour le terminal
ANSI_GREEN = '\033[92m'
ANSI_BLUE = '\033[94m'
ANSI_RED = '\033[91m'
ANSI_RESET = '\033[0m'

def load_yaml(path):
    """Charge la configuration."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"Erreur: Fichier introuvable {path}")

def get_cookies(path):
    """Charge les cookies (détection auto format Netscape ou clé=valeur)."""
    if not os.path.exists(path):
        return {}

    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    cookies = {}
    if '\t' in content: # Format Netscape (supposé si tabulations présentes)
        for line in content.splitlines():
            parts = line.strip().split('\t')
            if len(parts) >= 7 and not line.startswith('#'):
                cookies[parts[5]] = parts[6]
    else: # Format simple (clé=val; clé=val)
        for pair in content.replace('; ', ';').split(';'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                cookies[k] = v
    return cookies

def get_calendar_service(config):
    """Authentification Google OAuth2."""
    creds = None
    token_path = 'config/token.json'

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(config['calendar']['credentials_path'], SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())

    return build('calendar', 'v3', credentials=creds)

def fetch_doctolib(config, start_date):
    """Récupère et nettoie les RDV Doctolib pour la semaine."""
    api = config['api']
    start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = start_dt + timedelta(days=7) - timedelta(seconds=1)

    params = {
        'agenda_ids': api['agenda_ids'],
        'start_date': start_dt.strftime(api['date_format']),
        'end_date': end_dt.strftime(api['date_format']),
        'view': 'week',
        'include_patients': 'true'
    }

    # Récupération
    cookies = get_cookies(api['cookie_path'])
    if not cookies:
        print("Attention: Aucun cookie chargé.")

    resp = requests.get(api['url'], params=params, cookies=cookies,
                        headers={'User-Agent': api.get('user_agent', 'Mozilla/5.0')}, timeout=10)
    resp.raise_for_status()

    # Nettoyage
    clean_rdvs = []
    for item in resp.json().get('data', []):
        if item.get('status', 'confirmed').lower() == 'deleted':
            continue

        is_new = item.get('new_patient', False)
        clean_rdvs.append({
            'start': item.get('start_date'),
            'end': item.get('end_date'),
            'new_patient': is_new,
            'summary': "Nouveau patient" if is_new else "Suivi",
            'status': item.get('status', 'confirmed')
        })
    return [r for r in clean_rdvs if r['start'] and r['end']]

def fetch_google_events(service, calendar_id, start_date):
    """Récupère les événements existants de la semaine pour comparaison."""
    start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d")
    t_min = start_dt.replace(hour=0, minute=0, second=0).isoformat() + 'Z'
    t_max = (start_dt + timedelta(days=7)).replace(hour=0, minute=0, second=0).isoformat() + 'Z'

    events = service.events().list(calendarId=calendar_id, timeMin=t_min, timeMax=t_max,
                                   singleEvents=True).execute().get('items', [])

    mapping = {}
    for e in events:
        desc = e.get('description', '')
        if 'SYNC_KEY:' in desc:
            # Extraction de la clé unique
            mapping[desc.split('SYNC_KEY:')[1].strip()] = e
    return mapping

def _create_event_body(rdv, key, notif_config, loc):
    """Crée le corps de l'événement Google (Helper)."""
    notif_std, notif_first, last_day = notif_config
    day = rdv['start'].split('T')[0]

    mins = notif_first if (day != last_day and notif_first > 0) else notif_std

    body = {
        'summary': f"{rdv['summary']} [{rdv['status']}]",
        'description': f"Synchronisé depuis Doctolib. SYNC_KEY: {key}",
        'start': {'dateTime': rdv['start'], 'timeZone': 'Europe/Paris'},
        'end': {'dateTime': rdv['end'], 'timeZone': 'Europe/Paris'},
        'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': mins}]} if mins > 0 else {'useDefault': True}
    }
    if loc:
        body['location'] = loc

    return body, day

def _print_sync_stats(week_date, total_rdvs, stats, delete_count):
    """Affiche le résumé de la synchronisation avec couleurs."""
    s_add = f"{ANSI_GREEN}+{stats['add']} créés{ANSI_RESET}" if stats['add'] > 0 else f"+{stats['add']} créés"
    s_upd = f"{ANSI_BLUE}~{stats['upd']} maj{ANSI_RESET}" if stats['upd'] > 0 else f"~{stats['upd']} maj"
    s_del = f"{ANSI_RED}-{delete_count} supprimés{ANSI_RESET}" if delete_count > 0 else f"-{delete_count} supprimés"

    print(f"Semaine du {week_date} : {total_rdvs} RDVs | {s_add} / {s_upd} / {s_del}")

def sync_week(service, config, rdvs, existing_map, week_date):
    """Logique principale de synchronisation (Création, MAJ, Suppression)."""
    # Extraction groupée de la config pour réduire les variables locales (Fix R0914)
    notif_cfg = (config['config'].get('notification', 30), config['config'].get('first_of_day', 0))
    loc = config['config'].get('localisation', '').strip()

    # Tri nécessaire pour la logique "premier rdv de la journée"
    rdvs.sort(key=lambda x: x['start'])

    last_day = None
    to_delete = set(existing_map.keys())
    stats = {'add': 0, 'upd': 0}

    for rdv in rdvs:
        key = f"{rdv['start']}|{rdv['end']}|{rdv['new_patient']}"

        # Délégation de la création du corps à une fonction auxiliaire
        body, last_day = _create_event_body(rdv, key, (*notif_cfg, last_day), loc)

        # Logique CRUD
        if key in existing_map:
            to_delete.discard(key) # On garde cet événement
            ev = existing_map[key]
            # On met à jour seulement si nécessaire (optimisation quota API)
            if ev.get('location', '').strip() != loc or ev.get('reminders') != body['reminders']:
                service.events().update(calendarId=config['calendar']['id'], eventId=ev['id'], body=body).execute()
                stats['upd'] += 1
        else:
            service.events().insert(calendarId=config['calendar']['id'], body=body).execute()
            stats['add'] += 1

    # Suppression des événements qui ne sont plus dans Doctolib
    for k in to_delete:
        service.events().delete(calendarId=config['calendar']['id'], eventId=existing_map[k]['id']).execute()

    _print_sync_stats(week_date, len(rdvs), stats, len(to_delete))

def main():
    """Fonction principale du script."""
    parser = argparse.ArgumentParser()
    parser.add_argument('-w', '--weeks', type=int, default=1)
    args = parser.parse_args()

    config = load_yaml("config/config.yaml")

    try:
        service = get_calendar_service(config)
    except (ValueError, OSError, HttpError) as e:
        sys.exit(f"Erreur Auth Google: {e}")

    monday = datetime.date.today() - timedelta(days=datetime.date.today().weekday())

    print(f"Synchronisation sur {args.weeks} semaine(s)...")

    for i in range(args.weeks):
        w_start = (monday + timedelta(weeks=i)).strftime("%Y-%m-%d")

        try:
            rdvs = fetch_doctolib(config, w_start)
            existing = fetch_google_events(service, config['calendar']['id'], w_start)
            sync_week(service, config, rdvs, existing, w_start)
        except (requests.RequestException, HttpError) as e:
            print(f"Erreur semaine {w_start}: {e}")

if __name__ == '__main__':
    main()
