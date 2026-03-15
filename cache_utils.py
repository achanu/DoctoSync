"""Utilitaires de cache partagés entre doctosync et docto_heatmap.

Le cache stocke les RDVs bruts (confirmés et annulés) indexés par semaine.
Seul doctosync.py alimente le cache lors de la synchro ; docto_heatmap.py
le lit pour ses analyses prévisionnelles sans jamais l'écrire depuis la synchro.
"""

import json
import os
from typing import Any

_CACHE_VERSION = 2
_CACHE_FILE_DEFAULT = 'cache/.heatmap_cache.json'

_ANSI_BLUE = '\033[94m'
_ANSI_GREEN = '\033[92m'
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
