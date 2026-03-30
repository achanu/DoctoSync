"""Utilitaires partagés entre doctosync et docto_heatmap.

Contient le cache RDVs (indexé par semaine), les constantes ANSI, et les
fonctions de configuration, cookies et fetch Doctolib communes aux deux scripts.
"""

import datetime
from datetime import timedelta
import json
import os
import sys
from typing import Any

import browser_cookie3
import requests
import yaml

_CACHE_VERSION = 2
_CACHE_FILE_DEFAULT = 'cache/.heatmap_cache.json'

_ANSI_BLUE = '\033[94m'
_ANSI_GREEN = '\033[92m'
_ANSI_RED = '\033[91m'
_ANSI_RESET = '\033[0m'


def load_cache(path: str) -> dict[str, list[dict[str, Any]]]:
    """Charge le cache depuis un fichier JSON.

    Si la version du cache est inférieure à _CACHE_VERSION, le cache est
    considéré obsolète et un dict vide est retourné (re-fetch forcé).

    Args:
        path: Chemin vers le fichier de cache.

    Returns:
        Dictionnaire {week_start: [rdv, ...]} sans la clé '_version'.
    """
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if data.get('_version', 1) < _CACHE_VERSION:
        print(
            f'  {_ANSI_BLUE}Cache obsolète '
            f'(v{data.get("_version", 1)} → v{_CACHE_VERSION}), '
            f're-fetch complet.{_ANSI_RESET}'
        )
        return {}

    return {k: v for k, v in data.items() if k != '_version'}


def save_cache(
        path: str,
        cache: dict[str, list[dict[str, Any]]],
) -> None:
    """Persiste le cache dans un fichier JSON avec versioning.

    Crée le répertoire parent si nécessaire.

    Args:
        path: Chemin vers le fichier de cache.
        cache: Dictionnaire {week_start: [rdv, ...]} à sauvegarder.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    data = {'_version': _CACHE_VERSION, **cache}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
        except Exception:  # pylint: disable=broad-exception-caught
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


def fetch_recurring_events(
        config: dict[str, Any],
        start_date: str,
        cookies: Any,
) -> list[dict[str, Any]]:
    """Récupère les périodes d'ouverture et blocages pour une semaine donnée.

    Args:
        config: Configuration contenant les paramètres de l'API Doctolib.
        start_date: Date de début de semaine au format 'YYYY-MM-DD'.
        cookies: Cookies d'authentification Doctolib.

    Returns:
        Liste des événements (type 'open' ou 'blck') pour la semaine.

    Raises:
        requests.RequestException: En cas d'erreur réseau ou HTTP.
    """
    api = config['api']
    url = api['url'].replace('/appointments', '/recurring_events')
    start_dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = start_dt + timedelta(days=6)

    params = {
        'agenda_ids': api['agenda_ids'],
        'start_date': start_dt.strftime(api['date_format']),
        'end_date': end_dt.strftime('%Y-%m-%d 23:59:59'),
        'view': 'week',
    }

    resp = requests.get(
        url,
        params=params,
        cookies=cookies,
        headers={'User-Agent': api.get('user_agent', 'Mozilla/5.0')},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get('data', [])


def fetch_doctolib(
        config: dict[str, Any],
        start_date: str,
        cookies: Any,
) -> list[dict[str, Any]]:
    """Récupère les RDV Doctolib pour une semaine donnée, annulations incluses.

    Les RDVs annulés sont conservés (marqués cancelled=True) afin de permettre
    l'analyse du taux d'annulation.

    Args:
        config: Configuration contenant les paramètres de l'API Doctolib.
        start_date: Date de début de la semaine au format 'YYYY-MM-DD'.
        cookies: Cookies d'authentification Doctolib.

    Returns:
        Liste de RDVs, chacun sous forme de dictionnaire avec les champs :
        start, end, new_patient, status, cancelled, created_at.

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

    rdvs = []
    for item in resp.json().get('data', []):
        status = item.get('status', 'confirmed').lower()
        rdvs.append({
            'start': item.get('start_date'),
            'end': item.get('end_date'),
            'new_patient': item.get('new_patient', False),
            'status': status,
            'cancelled': status in ('deleted', 'no_show_but_ok'),
            'created_at': item.get('created_at'),
        })
    return [r for r in rdvs if r['start'] and r['end']]
