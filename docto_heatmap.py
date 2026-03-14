#!/usr/bin/env python
"""Analyse des rendez-vous Doctolib sous forme de heatmaps.

Génère deux heatmaps PNG à partir des N dernières semaines de RDVs :
  - Mensuelle    : fréquence par jour du mois (1-31), agrégée.
  - Hebdomadaire : fréquence par créneau horaire × jour de semaine.

Les données des semaines passées sont mises en cache localement pour éviter
des requêtes répétées à l'API Doctolib.

Utilisation :
    python docto_heatmap.py -w 12
    python docto_heatmap.py -w 12 --type new followup
    python docto_heatmap.py -w 8 -r 60 --no-cache
"""

import argparse
import datetime
from datetime import timedelta
import json
import os
import sys
from typing import Any

import browser_cookie3
import matplotlib.pyplot as plt
import pandas as pd
import requests
import seaborn as sns
import yaml

_DAYS_FR = ['Lun', 'Mar', 'Mer', 'Jeu', 'Ven', 'Sam', 'Dim']
_CACHE_FILE_DEFAULT = 'cache/.heatmap_cache.json'
_OUTPUT_DIR_DEFAULT = 'output'

# Mapping type de RDV → (suffixe de titre, préfixe de fichier).
# Extensible : ajouter une entrée ici suffit pour supporter un nouveau filtre.
_RDV_TYPES: dict[str, tuple[str, str]] = {
    'all': ('', 'all'),
    'new': (' — Nouveaux patients', 'new'),
    'followup': (' — Suivis', 'followup'),
}

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
            'status': item.get('status', 'confirmed'),
        })
    return [r for r in clean_rdvs if r['start'] and r['end']]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def load_cache(path: str) -> dict[str, list[dict[str, Any]]]:
    """Charge le cache depuis un fichier JSON.

    Args:
        path: Chemin vers le fichier de cache.

    Returns:
        Dictionnaire {week_start: [rdv, ...]} ou dict vide si inexistant.
    """
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_cache(
        path: str,
        cache: dict[str, list[dict[str, Any]]],
) -> None:
    """Persiste le cache dans un fichier JSON.

    Crée le répertoire parent si nécessaire.

    Args:
        path: Chemin vers le fichier de cache.
        cache: Dictionnaire {week_start: [rdv, ...]} à sauvegarder.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Récupération
# ---------------------------------------------------------------------------

def get_past_week_starts(n: int) -> list[str]:
    """Renvoie les dates de début des n semaines passées (lundis).

    Les semaines sont ordonnées de la plus ancienne à la plus récente.
    La semaine courante est exclue.

    Args:
        n: Nombre de semaines passées à inclure.

    Returns:
        Liste de dates au format 'YYYY-MM-DD'.
    """
    today = datetime.date.today()
    current_monday = today - timedelta(days=today.weekday())
    return [
        (current_monday - timedelta(weeks=i)).strftime('%Y-%m-%d')
        for i in range(n, 0, -1)
    ]


def fetch_all_appointments(
        config: dict[str, Any],
        cookies: Any,
        week_starts: list[str],
        cache_path: str | None,
) -> list[dict[str, Any]]:
    """Récupère tous les RDV pour les semaines données, avec cache.

    Les semaines déjà présentes dans le cache ne sont pas re-requêtées.
    Le cache est mis à jour après chaque nouvelle requête réussie.

    Args:
        config: Configuration globale du script.
        cookies: Cookies d'authentification Doctolib.
        week_starts: Liste de dates de début de semaine 'YYYY-MM-DD'.
        cache_path: Chemin du fichier de cache JSON, ou None pour désactiver.

    Returns:
        Liste agrégée de tous les RDVs sur la période.
    """
    cache: dict[str, list[dict[str, Any]]] = (
        load_cache(cache_path) if cache_path else {}
    )
    cache_updated = False
    all_rdvs: list[dict[str, Any]] = []

    for week_start in week_starts:
        if cache_path and week_start in cache:
            rdvs = cache[week_start]
            print(
                f'  Semaine {week_start} : '
                f'{len(rdvs)} RDVs {_ANSI_BLUE}(cache){_ANSI_RESET}.'
            )
        else:
            try:
                rdvs = fetch_doctolib(config, week_start, cookies)
                print(f'  Semaine {week_start} : {len(rdvs)} RDVs récupérés.')
                if cache_path:
                    cache[week_start] = rdvs
                    cache_updated = True
            except requests.RequestException as e:
                print(
                    f'  {_ANSI_RED}Erreur semaine {week_start}: '
                    f'{e}{_ANSI_RESET}'
                )
                rdvs = []

        all_rdvs.extend(rdvs)

    if cache_updated and cache_path:
        save_cache(cache_path, cache)
        print(
            f'  {_ANSI_GREEN}Cache mis à jour : {cache_path}{_ANSI_RESET}'
        )

    return all_rdvs


# ---------------------------------------------------------------------------
# Transformation
# ---------------------------------------------------------------------------

def _parse_appointments(
        rdvs: list[dict[str, Any]],
        slot_minutes: int,
) -> pd.DataFrame:
    """Convertit la liste de RDVs en DataFrame avec colonnes analytiques.

    Chaque RDV produit une seule ligne. La heatmap hebdomadaire se charge
    d'étendre les créneaux selon la durée réelle via start_slot/end_slot.

    Args:
        rdvs: Liste de RDVs nettoyés issus de fetch_doctolib.
        slot_minutes: Résolution des créneaux en minutes.

    Returns:
        DataFrame avec colonnes :
            day_of_month, weekday, start_slot, end_slot, new_patient.
    """
    rows = []
    for rdv in rdvs:
        dt_start = datetime.datetime.fromisoformat(rdv['start'])
        dt_end = datetime.datetime.fromisoformat(rdv['end'])
        start_min = dt_start.hour * 60 + dt_start.minute
        end_min = dt_end.hour * 60 + dt_end.minute
        start_slot = start_min // slot_minutes
        # Le créneau de fin : dernier créneau occupé (fin exclusive → -1 min).
        end_slot = (
            (end_min - 1) // slot_minutes
            if end_min > start_min
            else start_slot
        )
        rows.append({
            'day_of_month': dt_start.day,
            'weekday': dt_start.weekday(),  # 0 = Lundi
            'start_slot': start_slot,
            'end_slot': end_slot,
            'new_patient': rdv['new_patient'],
        })
    return pd.DataFrame(
        rows,
        columns=['day_of_month', 'weekday', 'start_slot', 'end_slot', 'new_patient'],
    )


def _filter_by_type(df: pd.DataFrame, rdv_type: str) -> pd.DataFrame:
    """Filtre le DataFrame selon le type de RDV.

    Args:
        df: DataFrame issu de _parse_appointments.
        rdv_type: Type parmi 'all', 'new', 'followup'.

    Returns:
        Sous-ensemble filtré du DataFrame (copie).
    """
    if rdv_type == 'new':
        return df[df['new_patient']].copy()
    if rdv_type == 'followup':
        return df[~df['new_patient']].copy()
    return df  # 'all' : pas de filtre, pas de copie inutile


def _monthly_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Agrège les RDVs par numéro de jour du mois (1-31).

    Args:
        df: DataFrame issu de _parse_appointments.

    Returns:
        DataFrame de forme (1, 31) avec le compte de RDVs par jour.
    """
    counts = (
        df.groupby('day_of_month')
        .size()
        .reindex(range(1, 32), fill_value=0)
    )
    return counts.to_frame().T


def _weekly_matrix(df: pd.DataFrame, slot_minutes: int) -> pd.DataFrame:
    """Agrège les RDVs en matrice créneaux × jours de semaine.

    Chaque RDV est étendu sur tous les créneaux couverts par sa durée réelle
    (de start_slot à end_slot inclus), ce qui reflète l'occupation effective
    du praticien plutôt que la seule heure de début.

    Exemple : un RDV de 09h00 à 10h30 avec résolution 30 min compte dans
    les créneaux 09h00, 09h30 et 10h00.

    Args:
        df: DataFrame issu de _parse_appointments.
        slot_minutes: Résolution des créneaux en minutes (pour le fallback
            vide).

    Returns:
        DataFrame de forme (N_slots, 7) avec le compte de RDVs par créneau
        et par jour de semaine. Les lignes couvrent la plage horaire observée.
    """
    if df.empty:
        fallback = range(
            8 * 60 // slot_minutes,
            20 * 60 // slot_minutes,
        )
        return pd.DataFrame(0, index=fallback, columns=range(7))

    expanded = [
        {'slot': slot, 'weekday': row['weekday']}
        for _, row in df.iterrows()
        for slot in range(int(row['start_slot']), int(row['end_slot']) + 1)
    ]
    df_exp = pd.DataFrame(expanded)
    pivot = (
        df_exp.groupby(['slot', 'weekday'])
        .size()
        .unstack(fill_value=0)
    )
    all_slots = range(df_exp['slot'].min(), df_exp['slot'].max() + 1)
    return pivot.reindex(index=all_slots, columns=range(7), fill_value=0)


def _slot_label(slot_index: int, slot_minutes: int) -> str:
    """Convertit un index de créneau en label horaire 'HH:MM'.

    Args:
        slot_index: Index de créneau.
        slot_minutes: Résolution des créneaux en minutes.

    Returns:
        Chaîne de caractères au format 'HH:MM'.
    """
    total_min = slot_index * slot_minutes
    h, m = divmod(total_min, 60)
    return f'{h:02d}:{m:02d}'


# ---------------------------------------------------------------------------
# Visualisation
# ---------------------------------------------------------------------------

def plot_monthly_heatmap(
        df: pd.DataFrame,
        title_suffix: str,
        output_path: str,
) -> None:
    """Génère et sauvegarde la heatmap mensuelle (jours 1-31).

    Args:
        df: DataFrame issu de _parse_appointments.
        title_suffix: Suffixe ajouté au titre (ex. période ou type de RDV).
        output_path: Chemin complet du fichier PNG de sortie.
    """
    matrix = _monthly_matrix(df)

    fig, ax = plt.subplots(figsize=(18, 2.5))
    sns.heatmap(
        matrix,
        ax=ax,
        cmap='YlOrRd',
        annot=True,
        fmt='.0f',
        linewidths=0.5,
        linecolor='white',
        cbar_kws={'label': 'Nombre de RDVs', 'shrink': 0.8},
        xticklabels=range(1, 32),
        yticklabels=False,
    )
    ax.set_title(
        f'Fréquence des RDVs par jour du mois{title_suffix}',
        fontsize=13,
        pad=10,
    )
    ax.set_xlabel('Jour du mois', fontsize=11)
    ax.tick_params(axis='x', labelsize=9)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(
        f'  {_ANSI_GREEN}Heatmap mensuelle sauvegardée : '
        f'{output_path}{_ANSI_RESET}'
    )


def plot_weekly_heatmap(
        df: pd.DataFrame,
        title_suffix: str,
        output_path: str,
        slot_minutes: int,
) -> None:
    """Génère et sauvegarde la heatmap hebdomadaire (créneaux × jours).

    Args:
        df: DataFrame issu de _parse_appointments.
        title_suffix: Suffixe ajouté au titre (ex. période ou type de RDV).
        output_path: Chemin complet du fichier PNG de sortie.
        slot_minutes: Résolution des créneaux en minutes (pour les labels).
    """
    matrix = _weekly_matrix(df, slot_minutes)
    y_labels = [_slot_label(i, slot_minutes) for i in matrix.index]

    fig, ax = plt.subplots(figsize=(10, max(6, len(y_labels) * 0.4)))
    sns.heatmap(
        matrix,
        ax=ax,
        cmap='YlOrRd',
        annot=True,
        fmt='.0f',
        linewidths=0.5,
        linecolor='white',
        cbar_kws={'label': 'Nombre de RDVs'},
        xticklabels=_DAYS_FR,
        yticklabels=y_labels,
    )
    ax.set_title(
        f'Fréquence des RDVs par créneau horaire{title_suffix}',
        fontsize=13,
        pad=10,
    )
    ax.set_xlabel('Jour de la semaine', fontsize=11)
    ax.set_ylabel('Créneau horaire', fontsize=11)
    ax.tick_params(axis='both', labelsize=9)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(
        f'  {_ANSI_GREEN}Heatmap hebdomadaire sauvegardée : '
        f'{output_path}{_ANSI_RESET}'
    )


def _generate_pair(
        df: pd.DataFrame,
        label: str,
        prefix: str,
        output_dir: str,
        slot_minutes: int,
) -> None:
    """Génère les deux heatmaps (mensuelle + hebdomadaire) pour un sous-ensemble.

    Args:
        df: DataFrame filtré issu de _parse_appointments.
        label: Suffixe de titre (ex. ' — Nouveaux patients (...)').
        prefix: Préfixe des noms de fichiers (ex. 'all', 'new', 'followup').
        output_dir: Répertoire de sortie.
        slot_minutes: Résolution des créneaux en minutes.
    """
    plot_monthly_heatmap(
        df, label,
        os.path.join(output_dir, f'heatmap_monthly_{prefix}.png'),
    )
    plot_weekly_heatmap(
        df, label,
        os.path.join(output_dir, f'heatmap_weekly_{prefix}.png'),
        slot_minutes,
    )


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main() -> None:
    """Point d'entrée principal du script d'analyse heatmap."""
    parser = argparse.ArgumentParser(
        description=(
            'Génère des heatmaps des RDVs Doctolib sur les N semaines passées.'
        ),
    )
    parser.add_argument(
        '-w', '--weeks',
        type=int,
        default=12,
        help='Nombre de semaines passées à analyser (défaut: 12).',
    )
    parser.add_argument(
        '-r', '--resolution',
        type=int,
        default=30,
        metavar='MINUTES',
        help='Résolution des créneaux horaires en minutes pour la heatmap '
             'hebdomadaire (défaut: 30).',
    )
    parser.add_argument(
        '-c', '--config',
        default='config/config.yaml',
        help='Chemin vers le fichier de configuration (défaut: config/config.yaml).',
    )
    parser.add_argument(
        '-o', '--output',
        default=_OUTPUT_DIR_DEFAULT,
        help=f'Répertoire de sortie des images PNG (défaut: {_OUTPUT_DIR_DEFAULT}).',
    )
    parser.add_argument(
        '--cache-file',
        default=_CACHE_FILE_DEFAULT,
        metavar='PATH',
        help=f'Chemin du fichier de cache JSON (défaut: {_CACHE_FILE_DEFAULT}).',
    )
    parser.add_argument(
        '--no-cache',
        action='store_true',
        help='Désactive le cache : toutes les semaines sont re-requêtées.',
    )
    parser.add_argument(
        '--type',
        nargs='+',
        choices=list(_RDV_TYPES),
        default=['all'],
        dest='rdv_types',
        metavar='TYPE',
        help=(
            'Type(s) de RDVs à analyser : all, new, followup. '
            'Accepte plusieurs valeurs (défaut: all). '
            'Exemple : --type all new followup'
        ),
    )
    args = parser.parse_args()

    if args.resolution <= 0 or 60 % args.resolution != 0:
        sys.exit(
            f'Erreur: la résolution ({args.resolution} min) doit être un '
            f'diviseur de 60 (ex. 15, 20, 30, 60).'
        )

    config = load_yaml(args.config)
    cache_path = None if args.no_cache else args.cache_file

    cookies = get_cookies(config['api']['cookie_path'])
    if not cookies:
        print(
            f'{_ANSI_RED}Attention: Aucun cookie chargé '
            f'(ni navigateur, ni fichier).{_ANSI_RESET}'
        )

    week_starts = get_past_week_starts(args.weeks)
    period_label = f' ({week_starts[0]} → {week_starts[-1]})'

    print(
        f'\n{_ANSI_BLUE}Analyse de {args.weeks} semaine(s) passées '
        f'[résolution: {args.resolution} min]...{_ANSI_RESET}'
    )
    print(f'Période : {week_starts[0]} → {week_starts[-1]}\n')

    all_rdvs = fetch_all_appointments(config, cookies, week_starts, cache_path)
    if not all_rdvs:
        sys.exit(
            f'\n{_ANSI_RED}Aucun RDV récupéré sur la période.{_ANSI_RESET}'
        )

    df = _parse_appointments(all_rdvs, args.resolution)

    n_new = df['new_patient'].sum()
    n_followup = len(df) - n_new
    print(
        f'\n{_ANSI_BLUE}Total : {len(df)} RDVs '
        f'({n_new} nouveaux patients, {n_followup} suivis).{_ANSI_RESET}\n'
    )

    os.makedirs(args.output, exist_ok=True)

    for rdv_type in args.rdv_types:
        label_suffix, prefix = _RDV_TYPES[rdv_type]
        subset = _filter_by_type(df, rdv_type)
        _generate_pair(
            subset,
            label_suffix + period_label,
            prefix,
            args.output,
            args.resolution,
        )


if __name__ == '__main__':
    main()
