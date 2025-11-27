#!/usr/bin/env python
# pylint: disable=C0301,R0914

"""
Script de synchronisation des rendez-vous Doctolib vers Google Calendar.
Utilise l'API Doctolib (via cookies) et l'API Google Calendar (via OAuth2).
"""

import argparse
import datetime
from datetime import timedelta
import os
import sys
import yaml
import requests

# Imports Google spécifiques
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

def load_config(config_path="config/config.yaml"):
    """
    Charge le fichier de configuration YAML.
    """
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Erreur fatale : Le fichier de configuration YAML n'a pas été trouvé à l'emplacement: {config_path}")
        # Quitter le script si la configuration n'est pas trouvée
        sys.exit(1)

# Fonction de gestion de cookies
def load_netscape_cookies(content_lines):
    """Charge les cookies au format Netscape et affiche la date d'expiration."""
    cookies = {}
    expiration_displayed = False

    for line in content_lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        fields = line.split('\t')

        # Le format Netscape doit avoir au moins 7 champs
        if len(fields) >= 7:
            # Nom (index 5) et Valeur (index 6)
            name = fields[5]
            value = fields[6]

            # 1. Extraction et affichage de l'expiration
            if not expiration_displayed:
                try:
                    expiry_timestamp = int(fields[4])
                    # Conversion du timestamp (secondes depuis l'époque)
                    expiry_date = datetime.datetime.fromtimestamp(expiry_timestamp)
                    print(f"-> Expiration du cookie principal: {expiry_date.strftime('%Y-%m-%d %H:%M:%S')}")
                    expiration_displayed = True
                except ValueError:
                    # Gérer les cas où le champ 4 n'est pas un entier valide
                    pass

            cookies[name] = value

    return cookies

def load_single_line_cookies(content):
    """Charge les cookies au format single-line (séparé par ';')."""
    cookies = {}

    # Le format est "key1=value1; key2=value2; ..."
    cookie_pairs = [pair.strip() for pair in content.split(';')]

    for pair in cookie_pairs:
        if '=' in pair:
            # Séparer la clé et la valeur une seule fois
            key, value = pair.split('=', 1)
            cookies[key.strip()] = value.strip()

    return cookies

def load_cookies_from_file(cookie_path):
    """Détecte automatiquement le format et charge les cookies."""
    try:
        with open(cookie_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Erreur: Fichier de cookies non trouvé à {cookie_path}.")
        return None

    # Détection du format :
    # Si le contenu a des tabulations ou commence par un commentaire, c'est Netscape.
    if '\t' in content or content.strip().startswith('#'):
        print("Format de cookie détecté: Netscape (multi-ligne).")
        return load_netscape_cookies(content.splitlines())

    # Si le contenu est court et contient des ';' et des '=', c'est single-ligne.
    if ';' in content and '=' in content:
        print("Format de cookie détecté: Single-ligne (séparé par des points-virgules).")
        return load_single_line_cookies(content)

    print("Format de cookie non reconnu ou vide.")
    return {}

def fetch_appointments(config, week_start_date_str):
    """
    Appelle l'API Doctolib, calcule les dates de début/fin et nettoie la réponse.

    Args:
        config (dict): Les paramètres chargés de config.yaml.
        week_start_date_str (str): La date de début de la semaine (ex: '2025-09-29').
    """
    api_config = config['api']
    base_url = api_config['url']

    # 1. Calcul des dates et formats
    try:
        # Assumer que l'entrée est YYYY-MM-DD
        start_date_obj = datetime.datetime.strptime(week_start_date_str, "%Y-%m-%d")
    except ValueError:
        print(f"Erreur: Le format de date '{week_start_date_str}' n'est pas valide (attendu YYYY-MM-DD).")
        return [], 0

    # Date de début (00:00:00)
    start_dt_full = start_date_obj.replace(hour=0, minute=0, second=0)
    # Date de fin (7 jours plus tard, 23:59:59)
    end_dt_full = start_dt_full + timedelta(days=7) - timedelta(seconds=1)

    date_format = api_config['date_format']
    start_date_param = start_dt_full.strftime(date_format)
    end_date_param = end_dt_full.strftime(date_format)

    # 2. Préparation des cookies et des paramètres
    cookies = load_cookies_from_file(api_config['cookie_path'])

    if cookies is None:
        # La fonction a renvoyé None en cas d'erreur fatale (FileNotFound)
        return [], 0

    # S'assurer qu'au moins un cookie est chargé avant de faire l'appel
    if not cookies:
        print("Avertissement: Aucun cookie valide chargé. L'appel API risque d'échouer.")

    # Définition des en-têtes de requête
    headers = {
        'User-Agent': api_config.get('user_agent', 'Mozilla/5.0'),
    }

    params = {
        'agenda_ids': api_config['agenda_ids'],
        'start_date': start_date_param,
        'end_date': end_date_param,
        'view': 'week',
        'include_patients': 'true'
    }

    # 3. Appel API et Nettoyage des données
    response = requests.get(base_url, params=params, cookies=cookies, headers=headers, timeout=10)
    response.raise_for_status() # Lève HTTPError si le code est 4xx ou 5xx

    json_response = response.json()
    data = json_response.get('data', [])

    # Nettoyage et transformation des clés
    cleaned_data = []
    ignored_count = 0

    for appointment in data:
        # NOTE: On utilise les clés exactes de l'API (start_date, new_patient)
        is_new_patient = appointment.get('new_patient', False) # Clé en minuscules

        # La clé est 'status' et la valeur par défaut est 'confirmed'
        status_value = appointment.get('status', 'confirmed')

        # 2. Filtrage
        if status_value.lower() == 'deleted':
            ignored_count += 1
            continue

        cleaned_rdv = {
            # On conserve les clés nettoyées (start_date, end_date) pour la suite
            'start_date': appointment.get('start_date'),
            'end_date': appointment.get('end_date'),
            'new_patient': is_new_patient,
            'status': status_value,

            # Titre basé sur la règle :
            'summary': "Nouveau patient" if is_new_patient else "Suivi"
        }

        # On vérifie que les dates sont présentes
        if cleaned_rdv['start_date'] and cleaned_rdv['end_date']:
            cleaned_data.append(cleaned_rdv)

    return cleaned_data, ignored_count

def get_start_of_current_week():
    """Calcule la date de début (Lundi) de la semaine en cours."""
    today = datetime.date.today()
    # today.weekday() retourne 0 pour Lundi, 6 pour Dimanche
    start_of_week = today - timedelta(days=today.weekday())
    return start_of_week

# Définir les scopes requis pour lire et écrire dans Google Calendar
SCOPES = ['https://www.googleapis.com/auth/calendar']

def get_calendar_service(config):
    """Gère l'authentification OAuth2 et retourne l'objet de service."""
    creds = None
    token_path = 'config/token.json'  # Fichier pour stocker le jeton de rafraîchissement

    # 1. Charger le jeton existant
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # 2. Si le jeton est expiré ou n'existe pas
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Demander un nouveau jeton si le jeton de rafraîchissement est valide
            creds.refresh(Request())
        else:
            # Lancer le flux d'authentification OAuth (ouverture d'une fenêtre de navigateur)
            flow = InstalledAppFlow.from_client_secrets_file(
                config['calendar']['credentials_path'], SCOPES)
            creds = flow.run_local_server(port=0)

        # 3. Sauvegarder les identifiants pour la prochaine exécution
        with open(token_path, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())

    # Construire et retourner l'objet de service
    return build('calendar', 'v3', credentials=creds)

def get_existing_events(service, config, week_start_date_str):
    """Récupère tous les événements Google Calendar pour la semaine donnée."""

    # 1. Calculer les bornes de temps (réutilise la logique de fetch_appointments)
    start_date_obj = datetime.datetime.strptime(week_start_date_str, "%Y-%m-%d")
    time_min = start_date_obj.replace(hour=0, minute=0, second=0).isoformat() + 'Z'  # Début de semaine
    time_max = (start_date_obj + timedelta(days=7)).replace(hour=0, minute=0, second=0).isoformat() + 'Z' # Fin de semaine

    calendar_id = config['calendar']['id']
    events_result = service.events().list(
        calendarId=calendar_id,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime'
    ).execute()

    events = events_result.get('items', [])

    # 2. Préparer les données pour la comparaison (liste d'IDs)
    # Nous utilisons l'ID de l'événement Google et une "clé unique" dans la description pour le suivi
    existing_events_map = {}

    for event in events:
        # Tenter de récupérer la clé de synchronisation unique stockée dans la description
        sync_key = event.get('description', '').split('SYNC_KEY:')[1].strip() if 'SYNC_KEY:' in event.get('description', '') else None

        if sync_key:
            existing_events_map[sync_key] = event

    return existing_events_map

def _build_event_body(rdv, sync_key, location_address, reminder_minutes):
    """Construit le dictionnaire de données pour l'événement Google Calendar."""
    event_summary = f"{rdv['summary']} [{rdv['status']}]"

    target_body = {
        'summary': event_summary,
        'description': f"Synchronisé depuis Doctolib. SYNC_KEY: {sync_key}",
        'start': {'dateTime': rdv['start_date'], 'timeZone': 'Europe/Paris'},
        'end': {'dateTime': rdv['end_date'], 'timeZone': 'Europe/Paris'},
    }

    # Ajouter la localisation si elle est définie
    if location_address:
        target_body['location'] = location_address

    # Ajouter les rappels personnalisés si la valeur est > 0
    if reminder_minutes > 0:
        target_body['reminders'] = {
            'useDefault': False,
            'overrides': [{'method': 'popup', 'minutes': reminder_minutes}],
        }
    else:
        # Si 0, utiliser le comportement par défaut de Google
        target_body['reminders'] = {'useDefault': True}

    return target_body

def synchronize_week(service, config, appointments_to_sync, existing_events_map):
    """
    Compare les RDV API et les événements Google pour créer, mettre à jour ou supprimer.
    Implémente la logique de notification intelligente et de localisation.
    """
    calendar_id = config['calendar']['id']
    default_reminder = config['config'].get('notification', 30)
    first_of_day_reminder = config['config'].get('first_of_day', 0)
    location_address = config['config'].get('localisation', '').strip()

    # Trier par heure de début pour la logique "premier du jour"
    appointments_to_sync.sort(key=lambda rdv: rdv['start_date'])

    last_day_processed = None
    # Initialiser tous les événements existants comme potentiellement à supprimer
    events_to_delete_keys = set(existing_events_map.keys())

    stats = {'create': 0, 'update': 0}

    for rdv in appointments_to_sync:
        sync_key = f"{rdv['start_date']}|{rdv['end_date']}|{rdv['new_patient']}"
        current_day = rdv['start_date'].split('T')[0]
        reminder_minutes = default_reminder

        if current_day != last_day_processed:
            if first_of_day_reminder > 0:
                reminder_minutes = first_of_day_reminder
            last_day_processed = current_day

        # Utilisation de la fonction auxiliaire pour réduire la complexité
        target_body = _build_event_body(rdv, sync_key, location_address, reminder_minutes)

        if sync_key in existing_events_map:
            current_event = existing_events_map[sync_key]
            events_to_delete_keys.discard(sync_key) # Marquer comme à garder

            # Vérification des différences (Localisation et Rappels)
            current_loc = current_event.get('location', '').strip()
            current_reminders = current_event.get('reminders', {})

            if current_loc != location_address or target_body['reminders'] != current_reminders:
                stats['update'] += 1
                try:
                    service.events().update(
                        calendarId=calendar_id, eventId=current_event['id'], body=target_body
                    ).execute()
                except HttpError as e:
                    print(f"Échec MAJ {current_event.get('summary')}: {e}")
        else:
            stats['create'] += 1
            try:
                service.events().insert(calendarId=calendar_id, body=target_body).execute()
            except HttpError as e:
                print(f"Échec création {rdv['summary']}: {e}")

    # Suppression des événements obsolètes
    print(f"\n-> Créations: {stats['create']}, Mises à jour: {stats['update']}, Suppressions: {len(events_to_delete_keys)}")

    for sync_key in events_to_delete_keys:
        try:
            service.events().delete(
                calendarId=calendar_id, eventId=existing_events_map[sync_key]['id']
            ).execute()
        except HttpError as e:
            print(f"Échec suppression {existing_events_map[sync_key].get('id')}: {e}")

def main():
    """
    Fonction principale du script de synchronisation.
    """

    # 1. Configuration des arguments
    parser = argparse.ArgumentParser(description="Synchronise les rendez-vous Doctolib vers Google Calendar.")
    parser.add_argument(
        '-w', '--weeks',
        type=int,
        default=1,
        help="Nombre de semaines à synchroniser à partir de la semaine en cours. La valeur par défaut est 1."
    )
    args = parser.parse_args()
    n_weeks = args.weeks

    print(f"Démarrage de la synchronisation pour {n_weeks} semaine(s) à partir de la semaine courante.")

    # 2. Chargement de la configuration
    config = load_config("config/config.yaml")

    # 3. Préparation du service Google Calendar
    try:
        calendar_service = get_calendar_service(config)
    except (RuntimeError, HttpError) as e:
        print(f"Erreur d'authentification Google Calendar: {e}")
        return

    # 4. Détermination du point de départ et Boucle de synchronisation
    current_week_start = get_start_of_current_week()

    # 5. Boucle de synchronisation
    for i in range(n_weeks):
        # Calcule la date de début pour la semaine actuelle dans la boucle
        week_start_date = current_week_start + timedelta(weeks=i)

        # Formatte la date comme requis par notre fonction fetch_appointments (YYYY-MM-DD)
        week_start_date_str = week_start_date.strftime("%Y-%m-%d")

        print(f"\n--- Synchronisation de la semaine du {week_start_date_str} ---")

        try:
            # Appel à l'API Doctolib pour récupérer les RDV
            appointments_to_sync, ignored_count = fetch_appointments(config, week_start_date_str)
        except requests.exceptions.HTTPError as e:
            print("\n--- ERREUR FATALE (Doctolib API) ---")
            print(f"Le script s'arrête en raison d'une erreur d'appel API: {e}")
            return

        # Récupérer les événements existants
        existing_events = get_existing_events(calendar_service, config, week_start_date_str)

        print(f"-> {len(appointments_to_sync)} RDV Doctolib / {len(existing_events)} RDV Google existants / {ignored_count} RDV ignorés.")

        # Exécuter la logique de synchronisation
        synchronize_week(calendar_service, config, appointments_to_sync, existing_events)

        print(f"-> {len(appointments_to_sync)} rendez-vous récupérés et prêts à être synchronisés.")

if __name__ == '__main__':
    main()
