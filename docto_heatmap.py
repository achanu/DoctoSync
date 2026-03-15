#!/usr/bin/env python
"""Analyse des rendez-vous Doctolib sous forme de heatmaps et indicateurs.

Génère plusieurs analyses visuelles à partir des N dernières semaines de RDVs :
  - Mensuelle    : fréquence par jour du mois (1-31).
  - Hebdomadaire : occupation par créneau × jour de semaine (durée complète).
  - Gaps         : créneaux libres entre RDVs consécutifs dans la journée.
  - Tendance     : évolution du volume de RDVs semaine par semaine.
  - Score        : attractivité composite des créneaux (remplissage + lead time
                   + fiabilité).
  - Simulation   : projection du nombre de RDVs pour un créneau cible.

Utilisation :
    python docto_heatmap.py -w 12
    python docto_heatmap.py --type all new followup --gaps --trend --score
    python docto_heatmap.py --simulate lun 09:00 --simulate-weeks 4
    python docto_heatmap.py -w 8 -r 60 --no-cache
    python docto_heatmap.py --forecast --forecast-weeks 4
"""

import argparse
import datetime
from datetime import timedelta
import os
import sys
from typing import Any

import browser_cookie3
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import pandas as pd
import requests
import seaborn as sns
import yaml

from cache_utils import _CACHE_FILE_DEFAULT, _CACHE_VERSION, load_cache, save_cache

_DAYS_FR = ['Lun', 'Mar', 'Mer', 'Jeu', 'Ven', 'Sam', 'Dim']
_OUTPUT_DIR_DEFAULT = 'output'

# Mapping type de RDV → (suffixe de titre, préfixe de fichier).
# Extensible : ajouter une entrée ici suffit pour supporter un nouveau filtre.
_RDV_TYPES: dict[str, tuple[str, str]] = {
    'all': ('', 'all'),
    'new': (' — Nouveaux patients', 'new'),
    'followup': (' — Suivis', 'followup'),
    'cancelled': (' — Annulations', 'cancelled'),
}

_ANSI_GREEN = '\033[92m'
_ANSI_BLUE = '\033[94m'
_ANSI_RED = '\033[91m'
_ANSI_RESET = '\033[0m'


# ---------------------------------------------------------------------------
# Configuration & cookies
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# API Doctolib
# ---------------------------------------------------------------------------

def fetch_doctolib(
        config: dict[str, Any],
        start_date: str,
        cookies: Any,
) -> list[dict[str, Any]]:
    """Récupère les RDV Doctolib pour une semaine donnée, annulations incluses.

    Contrairement à doctosync.py, les RDVs annulés sont conservés (marqués
    cancelled=True) afin de permettre l'analyse du taux d'annulation.

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


def load_future_from_cache(
        cache_path: str | None,
        n_future: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Charge la semaine courante et les semaines futures depuis le cache.

    Ne fait aucun appel API. Alimenté par doctosync.py lors de la synchro.
    Les semaines absentes du cache sont silencieusement ignorées.

    Args:
        cache_path: Chemin du fichier de cache, ou None si désactivé.
        n_future: Nombre de semaines futures à charger (courante incluse).

    Returns:
        Tuple (rdvs, liste des semaines effectivement chargées).
    """
    if not cache_path:
        return [], []
    cache = load_cache(cache_path)
    today = datetime.date.today()
    monday = today - timedelta(days=today.weekday())
    week_starts = [
        (monday + timedelta(weeks=i)).strftime('%Y-%m-%d')
        for i in range(n_future + 1)
    ]
    rdvs: list[dict[str, Any]] = []
    loaded: list[str] = []
    for ws in week_starts:
        if ws in cache:
            rdvs.extend(cache[ws])
            loaded.append(ws)
    return rdvs, loaded


def fetch_all_appointments(
        config: dict[str, Any],
        cookies: Any,
        week_starts: list[str],
        cache_path: str | None,
) -> list[dict[str, Any]]:
    """Récupère tous les RDV pour les semaines données, avec cache.

    Args:
        config: Configuration globale du script.
        cookies: Cookies d'authentification Doctolib.
        week_starts: Liste de dates de début de semaine 'YYYY-MM-DD'.
        cache_path: Chemin du fichier de cache JSON, ou None pour désactiver.

    Returns:
        Liste agrégée de tous les RDVs (confirmés et annulés) sur la période.
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

def _lead_time_days(rdv: dict[str, Any]) -> int | None:
    """Calcule le nombre de jours entre la réservation et le RDV.

    Args:
        rdv: RDV nettoyé avec clés 'start' et 'created_at'.

    Returns:
        Nombre de jours (≥ 0), ou None si created_at est absent/invalide.
    """
    created_str = rdv.get('created_at')
    if not created_str:
        return None
    try:
        dt_start = datetime.datetime.fromisoformat(rdv['start'])
        dt_created = datetime.datetime.fromisoformat(created_str)
        # Uniformiser la tz pour éviter les erreurs de soustraction.
        if (dt_start.tzinfo is None) != (dt_created.tzinfo is None):
            dt_start = dt_start.replace(tzinfo=None)
            dt_created = dt_created.replace(tzinfo=None)
        return max(0, (dt_start - dt_created).days)
    except (ValueError, TypeError):
        return None


def _parse_appointments(
        rdvs: list[dict[str, Any]],
        slot_minutes: int,
) -> pd.DataFrame:
    """Convertit la liste de RDVs en DataFrame avec colonnes analytiques.

    Chaque RDV produit une seule ligne. La heatmap hebdomadaire se charge
    d'étendre les créneaux selon la durée réelle via start_slot/end_slot.

    Args:
        rdvs: Liste de RDVs issus de fetch_doctolib (confirmés + annulés).
        slot_minutes: Résolution des créneaux en minutes.

    Returns:
        DataFrame avec colonnes :
            date, week_start, day_of_month, weekday,
            start_slot, end_slot, new_patient, cancelled, lead_time_days.
    """
    rows = []
    for rdv in rdvs:
        dt_start = datetime.datetime.fromisoformat(rdv['start'])
        dt_end = datetime.datetime.fromisoformat(rdv['end'])
        start_min = dt_start.hour * 60 + dt_start.minute
        end_min = dt_end.hour * 60 + dt_end.minute
        start_slot = start_min // slot_minutes
        end_slot = (
            (end_min - 1) // slot_minutes
            if end_min > start_min
            else start_slot
        )
        date_val = dt_start.date()
        week_monday = date_val - timedelta(days=date_val.weekday())
        rows.append({
            'date': date_val,
            'week_start': week_monday.isoformat(),
            'day_of_month': dt_start.day,
            'weekday': dt_start.weekday(),  # 0 = Lundi
            'start_slot': start_slot,
            'end_slot': end_slot,
            'new_patient': rdv.get('new_patient', False),
            'cancelled': rdv.get('cancelled', False),
            'lead_time_days': _lead_time_days(rdv),
        })
    return pd.DataFrame(rows)


def _filter_by_type(df: pd.DataFrame, rdv_type: str) -> pd.DataFrame:
    """Filtre le DataFrame selon le type de RDV.

    'all', 'new' et 'followup' excluent automatiquement les annulations.
    'cancelled' retourne uniquement les RDVs annulés.

    Args:
        df: DataFrame issu de _parse_appointments (tous statuts confondus).
        rdv_type: Type parmi 'all', 'new', 'followup', 'cancelled'.

    Returns:
        Sous-ensemble filtré du DataFrame (copie pour 'cancelled'/'new'/
        'followup', vue pour 'all').
    """
    if rdv_type == 'cancelled':
        return df[df['cancelled']].copy()
    df_conf = df[~df['cancelled']]
    if rdv_type == 'new':
        return df_conf[df_conf['new_patient']].copy()
    if rdv_type == 'followup':
        return df_conf[~df_conf['new_patient']].copy()
    return df_conf  # 'all' : confirmés uniquement


def _start_slot_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Compte les RDVs par (start_slot, weekday) sans expansion de durée.

    Utilisé pour les métriques basées sur le début du RDV (score, simulation).

    Args:
        df: DataFrame issu de _parse_appointments.

    Returns:
        DataFrame pivot (start_slot × weekday 0-6), 0 si absent.
    """
    if df.empty:
        return pd.DataFrame(dtype=int)
    pivot = df.groupby(['start_slot', 'weekday']).size().unstack(fill_value=0)
    return pivot.reindex(columns=range(7), fill_value=0)


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
    (de start_slot à end_slot inclus).

    Args:
        df: DataFrame issu de _parse_appointments.
        slot_minutes: Résolution des créneaux en minutes (pour le fallback).

    Returns:
        DataFrame de forme (N_slots, 7) avec le compte de RDVs par créneau.
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


def _gap_matrix(df: pd.DataFrame, slot_minutes: int) -> pd.DataFrame:
    """Compte les créneaux libres (trous) entre RDVs consécutifs par jour.

    Pour chaque paire de RDVs consécutifs dans la même journée, les créneaux
    libres entre la fin du premier et le début du second sont enregistrés.
    Le résultat montre les plages horaires structurellement sous-utilisées.

    Args:
        df: DataFrame de RDVs confirmés issu de _parse_appointments.
        slot_minutes: Résolution des créneaux en minutes (pour le fallback).

    Returns:
        DataFrame de forme (N_slots, 7) avec le compte de gaps par créneau.
    """
    free_slots: list[dict[str, int]] = []

    for _, day_df in df.groupby('date'):
        day_df = day_df.sort_values('start_slot')
        rows = day_df[['start_slot', 'end_slot', 'weekday']].to_dict('records')
        for prev, curr in zip(rows, rows[1:]):
            gap_start = prev['end_slot'] + 1
            gap_end = curr['start_slot'] - 1
            if gap_end >= gap_start:
                for slot in range(gap_start, gap_end + 1):
                    free_slots.append(
                        {'slot': slot, 'weekday': prev['weekday']}
                    )

    if not free_slots:
        fallback = range(
            8 * 60 // slot_minutes,
            20 * 60 // slot_minutes,
        )
        return pd.DataFrame(0, index=fallback, columns=range(7))

    df_free = pd.DataFrame(free_slots)
    pivot = (
        df_free.groupby(['slot', 'weekday'])
        .size()
        .unstack(fill_value=0)
    )
    all_slots = range(df_free['slot'].min(), df_free['slot'].max() + 1)
    return pivot.reindex(index=all_slots, columns=range(7), fill_value=0)


def _score_matrix(
        df_all: pd.DataFrame,
        n_weeks: int,
) -> pd.DataFrame:
    """Calcule le score composite d'attractivité [0-1] par (start_slot, weekday).

    Le score agrège trois composantes normalisées entre 0 et 1 :
      - fill_score   : taux de remplissage moyen par semaine.
      - lead_score   : lead time moyen (réservation anticipée = forte demande).
                       Absent si created_at non disponible dans les données.
      - reliability  : 1 - taux d'annulation.
    Les créneaux sans aucune donnée ont un score de 0.

    Args:
        df_all: DataFrame complet (confirmés + annulés).
        n_weeks: Nombre de semaines de la période analysée.

    Returns:
        DataFrame pivot (start_slot × weekday 0-6) de scores [0-1].
    """
    df_conf = df_all[~df_all['cancelled']]

    fill_matrix = _start_slot_matrix(df_conf)
    total_matrix = _start_slot_matrix(df_all)
    cancel_matrix = _start_slot_matrix(df_all[df_all['cancelled']])

    all_idx = fill_matrix.index.union(total_matrix.index)
    fill_matrix = fill_matrix.reindex(index=all_idx, columns=range(7), fill_value=0)
    total_matrix = total_matrix.reindex(index=all_idx, columns=range(7), fill_value=0)
    cancel_matrix = cancel_matrix.reindex(index=all_idx, columns=range(7), fill_value=0)

    fill_rate = fill_matrix / n_weeks
    fill_max = fill_rate.values.max()
    fill_norm = fill_rate / fill_max if fill_max > 0 else fill_rate

    # Fiabilité : 1 - taux d'annulation (NaN → fiable par défaut).
    reliability = (
        1 - cancel_matrix.div(total_matrix.replace(0, float('nan'))).fillna(0)
    )

    # Lead time : uniquement si les données created_at sont présentes.
    df_lead = df_conf[df_conf['lead_time_days'].notna()]
    if not df_lead.empty:
        lead_pivot = (
            df_lead.groupby(['start_slot', 'weekday'])['lead_time_days']
            .mean()
            .unstack(fill_value=0)
            .reindex(index=all_idx, columns=range(7), fill_value=0)
        )
        lead_max = lead_pivot.values.max()
        lead_norm = lead_pivot / lead_max if lead_max > 0 else lead_pivot
        score = (fill_norm + lead_norm + reliability) / 3
    else:
        score = (fill_norm + reliability) / 2

    # Zéro pour les créneaux sans aucune donnée.
    score[total_matrix == 0] = 0
    return score


def _cancel_rate_matrix(df_all: pd.DataFrame) -> pd.DataFrame:
    """Taux d'annulation historique par (start_slot, weekday).

    Args:
        df_all: DataFrame complet (confirmés + annulés).

    Returns:
        DataFrame pivot (start_slot × weekday 0-6) de taux [0-1].
        Valeur 0 si aucune donnée historique pour ce créneau.
    """
    total = _start_slot_matrix(df_all)
    if total.empty:
        return pd.DataFrame(dtype=float)
    cancel = _start_slot_matrix(df_all[df_all['cancelled']])
    cancel = cancel.reindex_like(total).fillna(0)
    return cancel.div(total.replace(0, float('nan'))).fillna(0)


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
        df: DataFrame issu de _parse_appointments (confirmés uniquement).
        title_suffix: Suffixe ajouté au titre.
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
        title_suffix: Suffixe ajouté au titre.
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


def plot_gap_heatmap(
        df: pd.DataFrame,
        title_suffix: str,
        output_path: str,
        slot_minutes: int,
) -> None:
    """Génère la heatmap des créneaux libres (trous entre RDVs).

    Chaque cellule indique combien de fois ce créneau horaire était un trou
    dans la journée (entre deux RDVs). Les zones chaudes signalent des plages
    structurellement sous-exploitées.

    Args:
        df: DataFrame de RDVs confirmés.
        title_suffix: Suffixe ajouté au titre.
        output_path: Chemin complet du fichier PNG de sortie.
        slot_minutes: Résolution des créneaux en minutes.
    """
    matrix = _gap_matrix(df, slot_minutes)
    y_labels = [_slot_label(i, slot_minutes) for i in matrix.index]

    fig, ax = plt.subplots(figsize=(10, max(6, len(y_labels) * 0.4)))
    sns.heatmap(
        matrix,
        ax=ax,
        cmap='Blues',
        annot=True,
        fmt='.0f',
        linewidths=0.5,
        linecolor='white',
        cbar_kws={'label': 'Nombre de trous observés'},
        xticklabels=_DAYS_FR,
        yticklabels=y_labels,
    )
    ax.set_title(
        f'Créneaux libres (trous entre RDVs){title_suffix}',
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
        f'  {_ANSI_GREEN}Heatmap gaps sauvegardée : '
        f'{output_path}{_ANSI_RESET}'
    )


def plot_trend(
        df_all: pd.DataFrame,
        title_suffix: str,
        output_path: str,
) -> None:
    """Génère un graphique de tendance : RDVs confirmés et annulés par semaine.

    Args:
        df_all: DataFrame complet (confirmés + annulés).
        title_suffix: Suffixe ajouté au titre.
        output_path: Chemin complet du fichier PNG de sortie.
    """
    weekly = (
        df_all.groupby(['week_start', 'cancelled'])
        .size()
        .unstack(fill_value=0)
        .rename(columns={False: 'Confirmés', True: 'Annulés'})
    )
    for col in ('Confirmés', 'Annulés'):
        if col not in weekly.columns:
            weekly[col] = 0

    fig, ax = plt.subplots(figsize=(max(10, len(weekly) * 0.7), 5))
    weekly['Confirmés'].plot(
        kind='bar', ax=ax, color='steelblue', label='Confirmés', width=0.6,
    )
    weekly['Annulés'].plot(
        kind='bar', ax=ax, color='salmon', label='Annulés', width=0.6,
        bottom=weekly['Confirmés'],
    )
    ax.set_title(
        f'Évolution du volume de RDVs par semaine{title_suffix}',
        fontsize=13,
        pad=10,
    )
    ax.set_xlabel('Semaine', fontsize=11)
    ax.set_ylabel('Nombre de RDVs', fontsize=11)
    ax.legend(fontsize=10)
    ax.tick_params(axis='x', rotation=45, labelsize=8)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(
        f'  {_ANSI_GREEN}Tendance sauvegardée : {output_path}{_ANSI_RESET}'
    )


def plot_score_heatmap(
        df_all: pd.DataFrame,
        title_suffix: str,
        output_path: str,
        slot_minutes: int,
        n_weeks: int,
) -> None:
    """Génère la heatmap de score composite d'attractivité des créneaux.

    Vert = créneau attractif (fort remplissage, forte anticipation, peu
    d'annulations). Rouge = créneau peu attractif ou sans données.

    Args:
        df_all: DataFrame complet (confirmés + annulés).
        title_suffix: Suffixe ajouté au titre.
        output_path: Chemin complet du fichier PNG de sortie.
        slot_minutes: Résolution des créneaux en minutes.
        n_weeks: Nombre de semaines analysées (pour le taux de remplissage).
    """
    matrix = _score_matrix(df_all, n_weeks)
    if matrix.empty:
        print(
            f'  {_ANSI_RED}Score : données insuffisantes.{_ANSI_RESET}'
        )
        return

    all_slots = range(matrix.index.min(), matrix.index.max() + 1)
    matrix = matrix.reindex(index=all_slots, columns=range(7), fill_value=0)
    y_labels = [_slot_label(i, slot_minutes) for i in matrix.index]

    fig, ax = plt.subplots(figsize=(10, max(6, len(y_labels) * 0.4)))
    sns.heatmap(
        matrix,
        ax=ax,
        cmap='RdYlGn',
        annot=True,
        fmt='.2f',
        linewidths=0.5,
        linecolor='white',
        vmin=0,
        vmax=1,
        cbar_kws={'label': 'Score (0 = faible, 1 = optimal)'},
        xticklabels=_DAYS_FR,
        yticklabels=y_labels,
    )
    ax.set_title(
        f"Score d'attractivité des créneaux{title_suffix}",
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
        f'  {_ANSI_GREEN}Score sauvegardé : {output_path}{_ANSI_RESET}'
    )


def simulate_slot(
        df_conf: pd.DataFrame,
        weekday: int,
        slot: int,
        n_past: int,
        n_future: int,
        output_path: str,
        slot_minutes: int,
) -> None:
    """Simule l'ouverture d'un créneau et projette les RDVs attendus.

    Si le créneau a des données historiques, la projection est directe.
    Sinon, une estimation est faite à partir des créneaux voisins (±1 et ±2
    slots, même jour de semaine).

    Args:
        df_conf: DataFrame de RDVs confirmés.
        weekday: Jour de semaine cible (0=Lun, ..., 6=Dim).
        slot: Index de créneau cible.
        n_past: Nombre de semaines dans la période historique.
        n_future: Nombre de semaines futures à projeter.
        output_path: Chemin complet du fichier PNG de sortie.
        slot_minutes: Résolution des créneaux en minutes.
    """
    fill_matrix = _start_slot_matrix(df_conf)
    day_name = _DAYS_FR[weekday]
    slot_time = _slot_label(slot, slot_minutes)

    has_data = (
        slot in fill_matrix.index
        and weekday in fill_matrix.columns
        and fill_matrix.loc[slot, weekday] > 0
    )

    if has_data:
        actual_count = int(fill_matrix.loc[slot, weekday])
        avg_per_week = actual_count / n_past
        confidence = 'élevée (données historiques directes)'
    else:
        actual_count = 0
        neighbors = [
            int(fill_matrix.loc[s, weekday])
            for delta in [-2, -1, 1, 2]
            if (s := slot + delta) in fill_matrix.index
            and weekday in fill_matrix.columns
        ]
        avg_per_week = (sum(neighbors) / len(neighbors) / n_past) if neighbors else 0
        confidence = (
            'estimée par interpolation (voisins ±2 créneaux)'
            if neighbors
            else 'indéterminée (aucune donnée dans ce secteur)'
        )

    projected = avg_per_week * n_future

    print(f'\n  {_ANSI_BLUE}Simulation — {day_name} {slot_time}{_ANSI_RESET}')
    print(
        f'  Historique  : {actual_count} RDVs sur {n_past} semaines '
        f'({avg_per_week:.2f}/sem)'
    )
    print(
        f'  Projection  : ~{projected:.0f} RDVs sur {n_future} semaine(s)'
    )
    print(f'  Confiance   : {confidence}')

    # Graphique contextuel : tous les créneaux du jour ciblé.
    if weekday in fill_matrix.columns:
        col_data = fill_matrix[weekday].copy()
    else:
        col_data = pd.Series({slot: 0}, dtype=int)

    if slot not in col_data.index:
        col_data.loc[slot] = 0
    col_data = col_data.sort_index()

    colors = ['tomato' if i == slot else 'steelblue' for i in col_data.index]
    y_labels_sim = [_slot_label(i, slot_minutes) for i in col_data.index]

    fig, ax = plt.subplots(figsize=(6, max(4, len(col_data) * 0.35)))
    ax.barh(
        range(len(col_data)),
        col_data.values,
        color=colors,
        edgecolor='white',
    )
    ax.set_yticks(range(len(col_data)))
    ax.set_yticklabels(y_labels_sim, fontsize=9)
    ax.invert_yaxis()
    ax.set_title(
        f'Simulation ouverture {day_name} {slot_time}\n'
        f'Projection {n_future} sem. : ~{projected:.0f} RDVs '
        f'({avg_per_week:.2f}/sem)',
        fontsize=11,
    )
    ax.set_xlabel("RDVs observés (historique)", fontsize=10)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(
        f'  {_ANSI_GREEN}Simulation sauvegardée : {output_path}{_ANSI_RESET}'
    )


def plot_fill_forecast(
        df_future: pd.DataFrame,
        hist_avg: float,
        output_path: str,
) -> None:
    """Taux de remplissage prévisionnel : RDVs confirmés par semaine future.

    Compare le nombre de RDVs déjà réservés à la moyenne historique.
    Rouge : <80 % | Orange : 80–100 % | Bleu : ≥ 100 %.

    Args:
        df_future: DataFrame des RDVs futurs issus de _parse_appointments.
        hist_avg: Moyenne historique de RDVs confirmés par semaine.
        output_path: Chemin complet du fichier PNG de sortie.
    """
    df_conf = df_future[~df_future['cancelled']]
    if df_conf.empty:
        print(
            f'  {_ANSI_RED}Forecast remplissage : '
            f'aucun RDV futur dans le cache.{_ANSI_RESET}'
        )
        return

    weekly = df_conf.groupby('week_start').size()
    pcts = (weekly / hist_avg * 100) if hist_avg > 0 else weekly * 0
    colors = [
        'tomato' if p < 80 else 'gold' if p < 100 else 'steelblue'
        for p in pcts.values
    ]

    fig, ax = plt.subplots(figsize=(max(8, len(weekly) * 1.5), 5))
    bars = ax.bar(
        range(len(weekly)), weekly.values,
        color=colors, edgecolor='white', width=0.6,
    )
    if hist_avg > 0:
        ax.axhline(
            hist_avg, color='gray', linestyle='--', linewidth=1.2,
            label=f'Moyenne historique ({hist_avg:.1f} RDVs/sem)',
        )
    for bar, pct in zip(bars, pcts.values):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.1,
            f'{pct:.0f}%',
            ha='center', va='bottom', fontsize=9,
        )
    ax.set_title(
        'Taux de remplissage prévisionnel (semaines à venir)',
        fontsize=13, pad=10,
    )
    ax.set_xlabel('Semaine', fontsize=11)
    ax.set_ylabel('RDVs confirmés', fontsize=11)
    ax.set_xticks(range(len(weekly)))
    ax.set_xticklabels(weekly.index, rotation=45, ha='right', fontsize=9)
    if hist_avg > 0:
        ax.legend(fontsize=10)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(
        f'  {_ANSI_GREEN}Forecast remplissage sauvegardé : '
        f'{output_path}{_ANSI_RESET}'
    )


def plot_cancel_risk_forecast(
        df_future: pd.DataFrame,
        cancel_rate: pd.DataFrame,
        output_path: str,
        slot_minutes: int,
) -> None:
    """Heatmap du risque d'annulation sur les créneaux futurs réservés.

    Pour chaque créneau (slot × jour) avec des RDVs futurs, estime le nombre
    de RDVs susceptibles d'être annulés d'après le taux historique.

    Args:
        df_future: DataFrame des RDVs futurs issus de _parse_appointments.
        cancel_rate: Taux d'annulation historique issu de _cancel_rate_matrix.
        output_path: Chemin complet du fichier PNG de sortie.
        slot_minutes: Résolution des créneaux en minutes (pour les labels).
    """
    df_fut_conf = df_future[~df_future['cancelled']]
    if df_fut_conf.empty or cancel_rate.empty:
        print(
            f'  {_ANSI_RED}Forecast risque : données insuffisantes.{_ANSI_RESET}'
        )
        return

    future_booked = _start_slot_matrix(df_fut_conf)
    all_idx = future_booked.index.union(cancel_rate.index)
    future_booked = future_booked.reindex(
        index=all_idx, columns=range(7), fill_value=0
    )
    cr = cancel_rate.reindex(index=all_idx, columns=range(7), fill_value=0)
    risk = future_booked.multiply(cr).where(
        future_booked > 0, other=float('nan')
    )

    if risk.isna().all().all():
        print(
            f'  {_ANSI_RED}Forecast risque : '
            f'aucun créneau futur réservé.{_ANSI_RESET}'
        )
        return

    all_slots = range(risk.index.min(), risk.index.max() + 1)
    risk = risk.reindex(index=all_slots, columns=range(7))
    y_labels = [_slot_label(i, slot_minutes) for i in risk.index]

    fig, ax = plt.subplots(figsize=(10, max(6, len(y_labels) * 0.4)))
    sns.heatmap(
        risk,
        ax=ax,
        cmap='YlOrRd',
        annot=True,
        fmt='.1f',
        linewidths=0.5,
        linecolor='white',
        cbar_kws={'label': 'RDVs à risque (attendus)'},
        xticklabels=_DAYS_FR,
        yticklabels=y_labels,
    )
    ax.set_title(
        "Risque d'annulation par créneau (semaines futures réservées)",
        fontsize=13, pad=10,
    )
    ax.set_xlabel('Jour de la semaine', fontsize=11)
    ax.set_ylabel('Créneau horaire', fontsize=11)
    ax.tick_params(axis='both', labelsize=9)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(
        f'  {_ANSI_GREEN}Forecast risque sauvegardé : '
        f'{output_path}{_ANSI_RESET}'
    )


def plot_charge_glissante(
        df_hist: pd.DataFrame,
        df_future: pd.DataFrame,
        output_path: str,
) -> None:
    """Vue glissante passé + futur : charge hebdomadaire sur un seul graphe.

    Bleu : semaines passées | Violet : semaine courante | Orange : futures.

    Args:
        df_hist: DataFrame historique (confirmés + annulés).
        df_future: DataFrame des RDVs futurs issus de _parse_appointments.
        output_path: Chemin complet du fichier PNG de sortie.
    """
    hist_conf = df_hist[~df_hist['cancelled']].groupby('week_start').size()
    fut_conf = (
        df_future[~df_future['cancelled']].groupby('week_start').size()
        if not df_future.empty else pd.Series(dtype=int)
    )

    today_monday = (
        datetime.date.today() - timedelta(days=datetime.date.today().weekday())
    ).isoformat()

    all_weeks = sorted(set(hist_conf.index) | set(fut_conf.index))
    counts = [
        int(hist_conf.get(w, fut_conf.get(w, 0)))
        for w in all_weeks
    ]
    colors = [
        'mediumpurple' if w == today_monday
        else 'darkorange' if w > today_monday
        else 'steelblue'
        for w in all_weeks
    ]

    fig, ax = plt.subplots(figsize=(max(10, len(all_weeks) * 0.9), 5))
    bars = ax.bar(
        range(len(all_weeks)), counts,
        color=colors, edgecolor='white', width=0.7,
    )
    for bar, val in zip(bars, counts):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.1,
            str(val),
            ha='center', va='bottom', fontsize=8,
        )

    legend_els = [
        Patch(facecolor='steelblue', label='Passé'),
        Patch(facecolor='mediumpurple', label='Semaine courante'),
        Patch(facecolor='darkorange', label='Futur (cache)'),
    ]
    ax.legend(handles=legend_els, fontsize=10)
    ax.set_title(
        'Charge hebdomadaire glissante (passé → futur)',
        fontsize=13, pad=10,
    )
    ax.set_xlabel('Semaine', fontsize=11)
    ax.set_ylabel('RDVs confirmés', fontsize=11)
    ax.set_xticks(range(len(all_weeks)))
    ax.set_xticklabels(all_weeks, rotation=45, ha='right', fontsize=8)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(
        f'  {_ANSI_GREEN}Charge glissante sauvegardée : '
        f'{output_path}{_ANSI_RESET}'
    )


def plot_carnet_projection(
        df_future: pd.DataFrame,
        cancel_rate: pd.DataFrame,
        output_path: str,
) -> None:
    """Projection du carnet : RDVs attendus après annulations par semaine.

    Pour chaque RDV futur confirmé, applique le taux d'annulation historique
    de son créneau (modèle Bernoulli) pour estimer l'espérance de RDVs
    maintenus et l'incertitude (±1 σ).

    Args:
        df_future: DataFrame des RDVs futurs issus de _parse_appointments.
        cancel_rate: Taux d'annulation historique issu de _cancel_rate_matrix.
        output_path: Chemin complet du fichier PNG de sortie.
    """
    df_fut_conf = df_future[~df_future['cancelled']]
    if df_fut_conf.empty:
        print(
            f'  {_ANSI_RED}Projection carnet : '
            f'aucun RDV futur dans le cache.{_ANSI_RESET}'
        )
        return

    rows = []
    for _, row in df_fut_conf.iterrows():
        slot = int(row['start_slot'])
        wd = int(row['weekday'])
        p_cancel = (
            float(cancel_rate.loc[slot, wd])
            if slot in cancel_rate.index and wd in cancel_rate.columns
            else 0.0
        )
        rows.append({
            'week_start': row['week_start'],
            'expected': 1 - p_cancel,
            'variance': p_cancel * (1 - p_cancel),
        })

    proj = (
        pd.DataFrame(rows)
        .groupby('week_start')
        .agg(
            booked=('expected', 'count'),
            expected=('expected', 'sum'),
            std=('variance', lambda v: v.sum() ** 0.5),
        )
    )

    fig, ax = plt.subplots(figsize=(max(8, len(proj) * 1.5), 5))
    x = range(len(proj))
    ax.bar(x, proj['booked'], color='lightsteelblue', label='RDVs réservés', width=0.6)
    ax.bar(x, proj['expected'], color='steelblue', label='RDVs attendus (proj.)', width=0.6)
    ax.errorbar(
        x, proj['expected'], yerr=proj['std'],
        fmt='none', color='black', capsize=4, linewidth=1.2,
        label='±1σ (incertitude)',
    )
    ax.set_title(
        'Projection du carnet de RDVs (après annulations estimées)',
        fontsize=13, pad=10,
    )
    ax.set_xlabel('Semaine', fontsize=11)
    ax.set_ylabel('RDVs', fontsize=11)
    ax.set_xticks(x)
    ax.set_xticklabels(proj.index, rotation=45, ha='right', fontsize=9)
    ax.legend(fontsize=10)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(
        f'  {_ANSI_GREEN}Projection carnet sauvegardée : '
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
        label: Suffixe de titre.
        prefix: Préfixe des noms de fichiers.
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
# Helpers CLI
# ---------------------------------------------------------------------------

def _parse_weekday(s: str) -> int:
    """Parse un jour de semaine depuis un nom FR ou un entier 0-6.

    Args:
        s: Nom du jour ('lun', 'mar', ...) ou entier en chaîne ('0'-'6').

    Returns:
        Entier 0 (Lundi) à 6 (Dimanche).

    Raises:
        SystemExit: Si la valeur est invalide.
    """
    names = {
        'lun': 0, 'mar': 1, 'mer': 2, 'jeu': 3,
        'ven': 4, 'sam': 5, 'dim': 6,
    }
    try:
        n = int(s)
        if 0 <= n <= 6:
            return n
        sys.exit(f'Erreur: jour invalide "{s}" (entier attendu entre 0 et 6)')
    except ValueError:
        key = s.lower()[:3]
        if key in names:
            return names[key]
        sys.exit(
            f'Erreur: jour invalide "{s}" '
            f'(attendu: lun, mar, mer, jeu, ven, sam, dim)'
        )


def _parse_time_to_slot(time_str: str, slot_minutes: int) -> int:
    """Convertit une heure 'HH:MM' en index de créneau.

    Args:
        time_str: Heure au format 'HH:MM'.
        slot_minutes: Résolution des créneaux en minutes.

    Returns:
        Index de créneau correspondant.

    Raises:
        SystemExit: Si le format est invalide.
    """
    try:
        h, m = map(int, time_str.split(':'))
        return (h * 60 + m) // slot_minutes
    except (ValueError, AttributeError):
        sys.exit(
            f'Erreur: heure invalide "{time_str}" (format attendu: HH:MM)'
        )


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main() -> None:
    """Point d'entrée principal du script d'analyse heatmap."""
    parser = argparse.ArgumentParser(
        description=(
            'Génère des analyses visuelles des RDVs Doctolib '
            'sur les N semaines passées.'
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
        help='Résolution des créneaux en minutes, diviseur de 60 (défaut: 30).',
    )
    parser.add_argument(
        '-c', '--config',
        default='config/config.yaml',
        help='Chemin vers la configuration (défaut: config/config.yaml).',
    )
    parser.add_argument(
        '-o', '--output',
        default=_OUTPUT_DIR_DEFAULT,
        help=f'Répertoire de sortie des PNG (défaut: {_OUTPUT_DIR_DEFAULT}).',
    )
    parser.add_argument(
        '--cache-file',
        default=_CACHE_FILE_DEFAULT,
        metavar='PATH',
        help=f'Chemin du cache JSON (défaut: {_CACHE_FILE_DEFAULT}).',
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
            'Type(s) de RDVs : all, new, followup, cancelled. '
            'Accepte plusieurs valeurs (défaut: all).'
        ),
    )
    parser.add_argument(
        '--gaps',
        action='store_true',
        help='Génère la heatmap des créneaux libres entre RDVs.',
    )
    parser.add_argument(
        '--trend',
        action='store_true',
        help='Génère le graphique de tendance semaine par semaine.',
    )
    parser.add_argument(
        '--score',
        action='store_true',
        help="Génère la heatmap de score composite d'attractivité.",
    )
    parser.add_argument(
        '--simulate',
        nargs=2,
        metavar=('JOUR', 'HEURE'),
        help=(
            'Simule l\'ouverture d\'un créneau. '
            'Ex: --simulate lun 09:00 ou --simulate 0 09:00'
        ),
    )
    parser.add_argument(
        '--simulate-weeks',
        type=int,
        default=4,
        metavar='N',
        help='Nombre de semaines futures pour la projection (défaut: 4).',
    )
    parser.add_argument(
        '--forecast',
        action='store_true',
        help=(
            'Active les analyses prévisionnelles sur les semaines futures '
            'du cache (alimenté par doctosync.py) : remplissage, risque '
            "d'annulation, charge glissante, projection du carnet."
        ),
    )
    parser.add_argument(
        '--forecast-weeks',
        type=int,
        default=4,
        metavar='N',
        help=(
            'Nombre de semaines futures à charger depuis le cache '
            '(semaine courante incluse, défaut: 4).'
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

    df_all = _parse_appointments(all_rdvs, args.resolution)
    df_conf = df_all[~df_all['cancelled']]
    n_conf = len(df_conf)
    n_cancelled = int(df_all['cancelled'].sum())
    n_new = int(df_conf['new_patient'].sum())
    n_followup = n_conf - n_new

    print(
        f'\n{_ANSI_BLUE}Total : {n_conf} RDVs confirmés '
        f'({n_new} nouveaux patients, {n_followup} suivis)'
        f'{f" + {n_cancelled} annulations" if n_cancelled else ""}.'
        f'{_ANSI_RESET}\n'
    )

    os.makedirs(args.output, exist_ok=True)

    # Heatmaps standard par type de RDV.
    for rdv_type in args.rdv_types:
        label_suffix, prefix = _RDV_TYPES[rdv_type]
        subset = _filter_by_type(df_all, rdv_type)
        _generate_pair(
            subset,
            label_suffix + period_label,
            prefix,
            args.output,
            args.resolution,
        )

    # Analyses complémentaires.
    if args.gaps:
        plot_gap_heatmap(
            df_conf,
            period_label,
            os.path.join(args.output, 'heatmap_gaps.png'),
            args.resolution,
        )

    if args.trend:
        plot_trend(
            df_all,
            period_label,
            os.path.join(args.output, 'trend.png'),
        )

    if args.score:
        plot_score_heatmap(
            df_all,
            period_label,
            os.path.join(args.output, 'heatmap_score.png'),
            args.resolution,
            args.weeks,
        )

    if args.simulate:
        weekday = _parse_weekday(args.simulate[0])
        slot = _parse_time_to_slot(args.simulate[1], args.resolution)
        day_str = _DAYS_FR[weekday]
        time_str = args.simulate[1].replace(':', 'h')
        simulate_slot(
            df_conf,
            weekday,
            slot,
            args.weeks,
            args.simulate_weeks,
            os.path.join(
                args.output,
                f'simulation_{day_str.lower()}_{time_str}.png',
            ),
            args.resolution,
        )

    if args.forecast:
        print(
            f'\n{_ANSI_BLUE}Analyses prévisionnelles '
            f'({args.forecast_weeks} semaine(s) futures)...{_ANSI_RESET}'
        )
        future_rdvs, future_weeks = load_future_from_cache(
            cache_path, args.forecast_weeks
        )
        if not future_rdvs:
            print(
                f'  {_ANSI_RED}Aucune donnée future dans le cache. '
                f'Lancez doctosync.py pour alimenter le cache.{_ANSI_RESET}'
            )
        else:
            print(
                f'  Semaines chargées : '
                f'{", ".join(future_weeks)}'
            )
            df_future = _parse_appointments(future_rdvs, args.resolution)
            cancel_rate = _cancel_rate_matrix(df_all)
            hist_avg = len(df_conf) / args.weeks

            plot_fill_forecast(
                df_future,
                hist_avg,
                os.path.join(args.output, 'forecast_remplissage.png'),
            )
            plot_cancel_risk_forecast(
                df_future,
                cancel_rate,
                os.path.join(args.output, 'forecast_risque_annulation.png'),
                args.resolution,
            )
            plot_charge_glissante(
                df_all,
                df_future,
                os.path.join(args.output, 'forecast_charge_glissante.png'),
            )
            plot_carnet_projection(
                df_future,
                cancel_rate,
                os.path.join(args.output, 'forecast_carnet_projection.png'),
            )


if __name__ == '__main__':
    main()
