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

import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import pandas as pd
import requests
import seaborn as sns
from docto_common import (
    _ANSI_BLUE,
    _ANSI_GREEN,
    _ANSI_RED,
    _ANSI_RESET,
    _CACHE_FILE_DEFAULT,
    fetch_doctolib,
    fetch_recurring_events,
    get_cookies,
    load_cache,
    load_yaml,
    save_cache,
)

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


def fetch_all_open_periods(
        config: dict[str, Any],
        cookies: Any,
        week_starts: list[str],
        cache_path: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Récupère les événements d'ouverture pour chaque semaine, avec cache.

    Args:
        config: Configuration globale du script.
        cookies: Cookies d'authentification Doctolib.
        week_starts: Liste de dates de début de semaine 'YYYY-MM-DD'.
        cache_path: Chemin du fichier de cache JSON dédié aux périodes
            d'ouverture, ou None pour désactiver.

    Returns:
        Dictionnaire {week_start: [events]} pour toutes les semaines.
    """
    cache: dict[str, list[dict[str, Any]]] = (
        load_cache(cache_path) if cache_path else {}
    )
    cache_updated = False
    result: dict[str, list[dict[str, Any]]] = {}

    for week_start in week_starts:
        if cache_path and week_start in cache:
            result[week_start] = cache[week_start]
            print(
                f'  Périodes {week_start} : '
                f'{_ANSI_BLUE}(cache){_ANSI_RESET}.'
            )
        else:
            try:
                events = fetch_recurring_events(config, week_start, cookies)
                result[week_start] = events
                print(
                    f'  Périodes {week_start} : {len(events)} événements.'
                )
                if cache_path:
                    cache[week_start] = events
                    cache_updated = True
            except requests.HTTPError as e:
                print(
                    f'  {_ANSI_RED}Erreur périodes {week_start}: '
                    f'{e}{_ANSI_RESET}'
                )
                if e.response is not None and e.response.status_code == 401:
                    print(
                        f'  {_ANSI_RED}Session expirée (401) — '
                        f'récupération des périodes abandonnée.{_ANSI_RESET}'
                    )
                    break
                result[week_start] = []
            except requests.RequestException as e:
                print(
                    f'  {_ANSI_RED}Erreur périodes {week_start}: '
                    f'{e}{_ANSI_RESET}'
                )
                result[week_start] = []

    if cache_updated and cache_path:
        save_cache(cache_path, cache)
        print(
            f'  {_ANSI_GREEN}Cache périodes mis à jour : '
            f'{cache_path}{_ANSI_RESET}'
        )

    return result


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


def _parse_open_periods(
        events: list[dict[str, Any]],
        slot_minutes: int,
) -> dict[int, set[int]]:
    """Calcule les créneaux ouverts nets par jour de semaine.

    Les événements 'open' définissent la disponibilité brute ; les 'blck'
    en soustraient une partie. Le résultat ne dépend que des heures de début
    et de fin de chaque événement dans la semaine interrogée.

    Args:
        events: Événements issus de fetch_recurring_events (une semaine).
        slot_minutes: Résolution des créneaux en minutes.

    Returns:
        Dictionnaire {weekday (0=Lun): ensemble des index de créneaux ouverts}.
    """
    open_windows: dict[int, list[tuple[int, int]]] = {}
    block_windows: dict[int, list[tuple[int, int]]] = {}

    for ev in events:
        dt_start = datetime.datetime.fromisoformat(ev['start_date'])
        dt_end = datetime.datetime.fromisoformat(ev['end_date'])
        wd = dt_start.weekday()
        start_min = dt_start.hour * 60 + dt_start.minute
        end_min = dt_end.hour * 60 + dt_end.minute
        if ev.get('type') == 'open':
            open_windows.setdefault(wd, []).append((start_min, end_min))
        elif ev.get('type') == 'blck':
            block_windows.setdefault(wd, []).append((start_min, end_min))

    result: dict[int, set[int]] = {}
    for wd, windows in open_windows.items():
        open_slots: set[int] = set()
        for start_m, end_m in windows:
            for s in range(
                start_m // slot_minutes,
                (end_m - 1) // slot_minutes + 1,
            ):
                open_slots.add(s)
        for start_m, end_m in block_windows.get(wd, []):
            for s in range(
                start_m // slot_minutes,
                (end_m - 1) // slot_minutes + 1,
            ):
                open_slots.discard(s)
        result[wd] = open_slots
    return result


def _open_count_matrix(
        events_by_week: dict[str, list[dict[str, Any]]],
        slot_minutes: int,
) -> pd.DataFrame:
    """Compte le nombre de semaines où chaque créneau était ouvert.

    Args:
        events_by_week: {week_start: [events]} issu de fetch_all_open_periods.
        slot_minutes: Résolution des créneaux en minutes.

    Returns:
        DataFrame (slot × weekday 0-6) contenant le nombre de semaines où
        chaque créneau était ouvert. 0 signifie jamais ouvert sur la période.
    """
    counts: dict[tuple[int, int], int] = {}
    for events in events_by_week.values():
        for wd, slots in _parse_open_periods(events, slot_minutes).items():
            for slot in slots:
                key = (slot, wd)
                counts[key] = counts.get(key, 0) + 1

    if not counts:
        return pd.DataFrame(dtype=int)

    series = pd.Series(counts)
    df = series.unstack(level=1, fill_value=0)
    return df.reindex(columns=range(7), fill_value=0)


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


def _gap_matrix(
        df: pd.DataFrame,
        slot_minutes: int,
        open_count: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Compte les créneaux libres (trous) entre RDVs consécutifs par jour.

    Pour chaque paire de RDVs consécutifs dans la même journée, les créneaux
    libres entre la fin du premier et le début du second sont enregistrés.
    Si open_count est fourni, seuls les gaps dans des créneaux ouverts sont
    conservés (les trous hors horaires d'ouverture sont ignorés).

    Args:
        df: DataFrame de RDVs confirmés issu de _parse_appointments.
        slot_minutes: Résolution des créneaux en minutes (pour le fallback).
        open_count: DataFrame (slot × weekday) issu de _open_count_matrix,
            ou None pour ne pas filtrer.

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
                    wd = prev['weekday']
                    if open_count is not None:
                        if (
                            slot not in open_count.index
                            or wd not in open_count.columns
                            or open_count.loc[slot, wd] == 0
                        ):
                            continue
                    free_slots.append({'slot': slot, 'weekday': wd})

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


def _score_matrix(  # pylint: disable=too-many-locals
        df_all: pd.DataFrame,
        n_weeks: int,
        open_count: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Calcule le score composite d'attractivité [0-1] par (start_slot, weekday).

    Le score agrège trois composantes normalisées entre 0 et 1 :
      - fill_score   : taux de remplissage moyen par semaine. Si open_count
                       est fourni, le dénominateur est le nombre de semaines
                       où ce créneau était ouvert (plus précis).
      - lead_score   : lead time moyen (réservation anticipée = forte demande).
                       Absent si created_at non disponible dans les données.
      - reliability  : 1 - taux d'annulation.
    Les créneaux sans aucune donnée (ou jamais ouverts) ont un score de 0.

    Args:
        df_all: DataFrame complet (confirmés + annulés).
        n_weeks: Nombre de semaines de la période (fallback si open_count absent).
        open_count: DataFrame (slot × weekday) issu de _open_count_matrix,
            ou None pour utiliser n_weeks comme dénominateur global.

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

    if open_count is not None:
        denom = (
            open_count
            .reindex(index=all_idx, columns=range(7), fill_value=0)
            .replace(0, float('nan'))
        )
        fill_rate = fill_matrix.div(denom).fillna(0)
    else:
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

    _, ax = plt.subplots(figsize=(18, 2.5))
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
        open_count: pd.DataFrame | None = None,
) -> None:
    """Génère et sauvegarde la heatmap hebdomadaire (créneaux × jours).

    Si open_count est fourni, les créneaux jamais ouverts sur la période
    sont grisés.

    Args:
        df: DataFrame issu de _parse_appointments.
        title_suffix: Suffixe ajouté au titre.
        output_path: Chemin complet du fichier PNG de sortie.
        slot_minutes: Résolution des créneaux en minutes (pour les labels).
        open_count: DataFrame (slot × weekday) issu de _open_count_matrix,
            ou None pour ne pas masquer.
    """
    matrix = _weekly_matrix(df, slot_minutes)

    closed_mask = None
    if open_count is not None:
        oc = open_count.reindex(
            index=matrix.index, columns=range(7), fill_value=0
        )
        closed_mask = oc == 0

    y_labels = [_slot_label(i, slot_minutes) for i in matrix.index]

    _, ax = plt.subplots(figsize=(10, max(6, len(y_labels) * 0.4)))
    if closed_mask is not None:
        ax.set_facecolor('#d8d8d8')
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
        mask=closed_mask,
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
        open_count: pd.DataFrame | None = None,
) -> None:
    """Génère la heatmap des créneaux libres (trous entre RDVs).

    Chaque cellule indique combien de fois ce créneau horaire était un trou
    dans la journée (entre deux RDVs). Les zones chaudes signalent des plages
    structurellement sous-exploitées.
    Si open_count est fourni, les gaps hors créneaux ouverts sont filtrés.

    Args:
        df: DataFrame de RDVs confirmés.
        title_suffix: Suffixe ajouté au titre.
        output_path: Chemin complet du fichier PNG de sortie.
        slot_minutes: Résolution des créneaux en minutes.
        open_count: DataFrame (slot × weekday) issu de _open_count_matrix,
            ou None pour ne pas filtrer.
    """
    matrix = _gap_matrix(df, slot_minutes, open_count)
    y_labels = [_slot_label(i, slot_minutes) for i in matrix.index]

    _, ax = plt.subplots(figsize=(10, max(6, len(y_labels) * 0.4)))
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


def plot_occupancy_heatmap(
        df_conf: pd.DataFrame,
        open_count: pd.DataFrame,
        title_suffix: str,
        output_path: str,
        slot_minutes: int,
) -> None:
    """Heatmap du taux d'occupation des créneaux ouverts.

    Chaque cellule affiche le taux de remplissage moyen du créneau :
    nb_rdvs / nb_semaines_ouvertes. Le dénominateur est historique : si un
    créneau n'était ouvert que 6 semaines sur 12, la division se fait sur 6.
    Les créneaux jamais ouverts sur la période apparaissent en gris.

    Args:
        df_conf: DataFrame des RDVs confirmés issu de _parse_appointments.
        open_count: DataFrame (slot × weekday 0-6) issu de _open_count_matrix,
            contenant le nombre de semaines où chaque créneau était ouvert.
        title_suffix: Suffixe ajouté au titre.
        output_path: Chemin complet du fichier PNG de sortie.
        slot_minutes: Résolution des créneaux en minutes.
    """
    if open_count.empty:
        print(
            f'  {_ANSI_RED}Occupation : aucune période d\'ouverture '
            f'disponible.{_ANSI_RESET}'
        )
        return

    slot_min = int(open_count.index.min())
    slot_max = int(open_count.index.max())
    if not df_conf.empty:
        slot_min = min(slot_min, int(df_conf['start_slot'].min()))
        slot_max = max(slot_max, int(df_conf['start_slot'].max()))

    fill_matrix = _start_slot_matrix(df_conf)
    open_full = open_count.reindex(
        index=range(slot_min, slot_max + 1),
        columns=range(7),
        fill_value=0,
    )

    rows = {}
    for slot in range(slot_min, slot_max + 1):
        row = {}
        for wd in range(7):
            n_open = int(open_full.loc[slot, wd])
            if n_open > 0:
                count = (
                    int(fill_matrix.loc[slot, wd])
                    if slot in fill_matrix.index and wd in fill_matrix.columns
                    else 0
                )
                row[wd] = count / n_open
            else:
                row[wd] = float('nan')
        rows[slot] = row

    matrix = pd.DataFrame(rows).T
    matrix.columns = range(7)

    y_labels = [_slot_label(i, slot_minutes) for i in matrix.index]

    _, ax = plt.subplots(figsize=(10, max(6, len(y_labels) * 0.4)))
    ax.set_facecolor('#d8d8d8')
    sns.heatmap(
        matrix,
        ax=ax,
        cmap='YlOrRd',
        annot=True,
        fmt='.2f',
        linewidths=0.5,
        linecolor='white',
        vmin=0,
        cbar_kws={'label': 'Taux de remplissage (RDVs / semaines ouvertes)'},
        xticklabels=_DAYS_FR,
        yticklabels=y_labels,
        mask=matrix.isna(),
    )
    ax.set_title(
        f"Taux d'occupation des créneaux ouverts{title_suffix}",
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
        f'  {_ANSI_GREEN}Occupation sauvegardée : {output_path}{_ANSI_RESET}'
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

    _, ax = plt.subplots(figsize=(max(10, len(weekly) * 0.7), 5))
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
        open_count: pd.DataFrame | None = None,
) -> None:
    """Génère la heatmap de score composite d'attractivité des créneaux.

    Vert = créneau attractif (fort remplissage, forte anticipation, peu
    d'annulations). Rouge = créneau peu attractif ou sans données.
    Les créneaux jamais ouverts sont grisés si open_count est fourni.

    Args:
        df_all: DataFrame complet (confirmés + annulés).
        title_suffix: Suffixe ajouté au titre.
        output_path: Chemin complet du fichier PNG de sortie.
        slot_minutes: Résolution des créneaux en minutes.
        n_weeks: Nombre de semaines analysées (fallback si open_count absent).
        open_count: DataFrame (slot × weekday) issu de _open_count_matrix,
            ou None pour utiliser n_weeks comme dénominateur global.
    """
    matrix = _score_matrix(df_all, n_weeks, open_count)
    if matrix.empty:
        print(
            f'  {_ANSI_RED}Score : données insuffisantes.{_ANSI_RESET}'
        )
        return

    all_slots = range(matrix.index.min(), matrix.index.max() + 1)
    matrix = matrix.reindex(index=all_slots, columns=range(7), fill_value=0)

    closed_mask = None
    if open_count is not None:
        oc = open_count.reindex(
            index=all_slots, columns=range(7), fill_value=0
        )
        closed_mask = oc == 0

    y_labels = [_slot_label(i, slot_minutes) for i in matrix.index]

    _, ax = plt.subplots(figsize=(10, max(6, len(y_labels) * 0.4)))
    if closed_mask is not None:
        ax.set_facecolor('#d8d8d8')
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
        mask=closed_mask,
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


def _compute_slot_stats(
        fill_matrix: pd.DataFrame,
        weekday: int,
        slot: int,
        n_past: int,
        open_count: pd.DataFrame | None = None,
) -> tuple[int, float, str]:
    """Calcule les statistiques historiques pour un créneau cible.

    Si open_count est fourni, le dénominateur est le nombre de semaines où
    ce créneau était ouvert (plus précis que n_past global).

    Args:
        fill_matrix: Matrice (start_slot × weekday) des RDVs confirmés.
        weekday: Jour de semaine cible (0=Lun, ..., 6=Dim).
        slot: Index de créneau cible.
        n_past: Nombre de semaines historiques (fallback si open_count absent).
        open_count: DataFrame (slot × weekday) issu de _open_count_matrix,
            ou None.

    Returns:
        Tuple (actual_count, avg_per_week, confidence).
    """
    def _denom(s: int, wd: int) -> int:
        if open_count is not None:
            if s in open_count.index and wd in open_count.columns:
                n = int(open_count.loc[s, wd])
                return n if n > 0 else n_past
        return n_past

    has_data = (
        slot in fill_matrix.index
        and weekday in fill_matrix.columns
        and fill_matrix.loc[slot, weekday] > 0
    )
    if has_data:
        actual_count = int(fill_matrix.loc[slot, weekday])
        return (
            actual_count,
            actual_count / _denom(slot, weekday),
            'élevée (données historiques directes)',
        )

    neighbors = [
        int(fill_matrix.loc[s, weekday])
        for delta in [-2, -1, 1, 2]
        if (s := slot + delta) in fill_matrix.index
        and weekday in fill_matrix.columns
    ]
    if neighbors:
        avg = sum(
            n / _denom(slot + delta, weekday)
            for delta, n in zip([-2, -1, 1, 2], neighbors)
        ) / len(neighbors)
        confidence = 'estimée par interpolation (voisins ±2 créneaux)'
    else:
        avg = 0.0
        confidence = 'indéterminée (aucune donnée dans ce secteur)'
    return 0, avg, confidence


def _save_simulation_chart(  # pylint: disable=too-many-locals
        fill_matrix: pd.DataFrame,
        target: tuple[int, int],
        projection: tuple[int, float, float],
        slot_minutes: int,
        output_path: str,
) -> None:
    """Génère et sauvegarde le graphique contextuel de simulation.

    Args:
        fill_matrix: Matrice (start_slot × weekday) des RDVs confirmés.
        target: Tuple (weekday, slot).
        projection: Tuple (n_future, avg_per_week, projected).
        slot_minutes: Résolution des créneaux en minutes.
        output_path: Chemin complet du fichier PNG de sortie.
    """
    weekday, slot = target
    n_future, avg_per_week, projected = projection
    day_name = _DAYS_FR[weekday]
    slot_time = _slot_label(slot, slot_minutes)

    col_data = fill_matrix[weekday].copy() if weekday in fill_matrix.columns \
        else pd.Series({slot: 0}, dtype=int)
    if slot not in col_data.index:
        col_data.loc[slot] = 0
    col_data = col_data.sort_index()

    colors = ['tomato' if i == slot else 'steelblue' for i in col_data.index]
    y_labels = [_slot_label(i, slot_minutes) for i in col_data.index]

    _, ax = plt.subplots(figsize=(6, max(4, len(col_data) * 0.35)))
    ax.barh(range(len(col_data)), col_data.values, color=colors, edgecolor='white')
    ax.set_yticks(range(len(col_data)))
    ax.set_yticklabels(y_labels, fontsize=9)
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
    print(f'  {_ANSI_GREEN}Simulation sauvegardée : {output_path}{_ANSI_RESET}')


def simulate_slot(  # pylint: disable=too-many-locals
        df_conf: pd.DataFrame,
        target: tuple[int, int],
        weeks: tuple[int, int],
        output_path: str,
        slot_minutes: int,
        open_count: pd.DataFrame | None = None,
) -> None:
    """Simule l'ouverture d'un créneau et projette les RDVs attendus.

    Si le créneau a des données historiques, la projection est directe.
    Sinon, une estimation est faite à partir des créneaux voisins (±1 et ±2
    slots, même jour de semaine).
    Si open_count est fourni, le dénominateur est le nombre de semaines où
    ce créneau était ouvert.

    Args:
        df_conf: DataFrame de RDVs confirmés.
        target: Tuple (weekday, slot) — jour (0=Lun..6=Dim) et index de
            créneau cible.
        weeks: Tuple (n_past, n_future) — nombre de semaines historiques et
            futures à projeter.
        output_path: Chemin complet du fichier PNG de sortie.
        slot_minutes: Résolution des créneaux en minutes.
        open_count: DataFrame (slot × weekday) issu de _open_count_matrix,
            ou None pour utiliser n_past comme dénominateur global.
    """
    weekday, slot = target
    n_past, n_future = weeks
    fill_matrix = _start_slot_matrix(df_conf)
    day_name = _DAYS_FR[weekday]
    slot_time = _slot_label(slot, slot_minutes)

    actual_count, avg_per_week, confidence = _compute_slot_stats(
        fill_matrix, weekday, slot, n_past, open_count
    )
    projected = avg_per_week * n_future

    print(f'\n  {_ANSI_BLUE}Simulation — {day_name} {slot_time}{_ANSI_RESET}')
    print(f'  Historique  : {actual_count} RDVs sur {n_past} semaines ({avg_per_week:.2f}/sem)')
    print(f'  Projection  : ~{projected:.0f} RDVs sur {n_future} semaine(s)')
    print(f'  Confiance   : {confidence}')

    _save_simulation_chart(
        fill_matrix, target, (n_future, avg_per_week, projected), slot_minutes, output_path
    )


def plot_fill_forecast(
        df_future: pd.DataFrame,
        hist_avg: float,
        output_path: str,
        open_slots_by_week: dict[str, int] | None = None,
) -> None:
    """Taux de remplissage prévisionnel : RDVs confirmés par semaine future.

    Compare le nombre de RDVs déjà réservés à la moyenne historique.
    Si open_slots_by_week est fourni, affiche aussi la capacité ouverte par
    semaine (créneaux disponibles) et annote le taux réel booked/open.
    Rouge : <80 % | Orange : 80–100 % | Bleu : ≥ 100 % (base : hist_avg).

    Args:
        df_future: DataFrame des RDVs futurs issus de _parse_appointments.
        hist_avg: Moyenne historique de RDVs confirmés par semaine.
        output_path: Chemin complet du fichier PNG de sortie.
        open_slots_by_week: {week_start: nb_créneaux_ouverts} ou None.
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

    _, ax = plt.subplots(figsize=(max(8, len(weekly) * 1.5), 5))

    if open_slots_by_week:
        open_vals = [
            open_slots_by_week.get(ws, 0) for ws in weekly.index
        ]
        ax.bar(
            range(len(weekly)), open_vals,
            color='#e0e0e0', edgecolor='white', width=0.6,
            label='Créneaux ouverts',
        )

    bars = ax.bar(
        range(len(weekly)), weekly.values,
        color=colors, edgecolor='white', width=0.6,
        label='RDVs confirmés',
    )
    if hist_avg > 0:
        ax.axhline(
            hist_avg, color='gray', linestyle='--', linewidth=1.2,
            label=f'Moyenne historique ({hist_avg:.1f} RDVs/sem)',
        )

    for i, (rect, pct, ws) in enumerate(
        zip(bars, pcts.values, weekly.index)
    ):
        label = f'{pct:.0f}%'
        if open_slots_by_week:
            n_open = open_slots_by_week.get(ws, 0)
            if n_open > 0:
                label += f'\n({weekly.iloc[i]/n_open*100:.0f}% cap.)'
        ax.text(
            rect.get_x() + rect.get_width() / 2,
            rect.get_height() + 0.1,
            label,
            ha='center', va='bottom', fontsize=8,
        )

    ax.set_title(
        'Taux de remplissage prévisionnel (semaines à venir)',
        fontsize=13, pad=10,
    )
    ax.set_xlabel('Semaine', fontsize=11)
    ax.set_ylabel('RDVs confirmés', fontsize=11)
    ax.set_xticks(range(len(weekly)))
    ax.set_xticklabels(weekly.index, rotation=45, ha='right', fontsize=9)
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

    _, ax = plt.subplots(figsize=(10, max(6, len(y_labels) * 0.4)))
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

    _, ax = plt.subplots(figsize=(max(10, len(all_weeks) * 0.9), 5))
    bars = ax.bar(
        range(len(all_weeks)), counts,
        color=colors, edgecolor='white', width=0.7,
    )
    for rect, val in zip(bars, counts):
        ax.text(
            rect.get_x() + rect.get_width() / 2,
            rect.get_height() + 0.1,
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

    _, ax = plt.subplots(figsize=(max(8, len(proj) * 1.5), 5))
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
        open_count: pd.DataFrame | None = None,
) -> None:
    """Génère les deux heatmaps (mensuelle + hebdomadaire) pour un sous-ensemble.

    Args:
        df: DataFrame filtré issu de _parse_appointments.
        label: Suffixe de titre.
        prefix: Préfixe des noms de fichiers.
        output_dir: Répertoire de sortie.
        slot_minutes: Résolution des créneaux en minutes.
        open_count: DataFrame (slot × weekday) issu de _open_count_matrix,
            ou None pour ne pas masquer les créneaux fermés.
    """
    plot_monthly_heatmap(
        df, label,
        os.path.join(output_dir, f'heatmap_monthly_{prefix}.png'),
    )
    plot_weekly_heatmap(
        df, label,
        os.path.join(output_dir, f'heatmap_weekly_{prefix}.png'),
        slot_minutes,
        open_count,
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

def _build_arg_parser() -> argparse.ArgumentParser:
    """Construit et retourne le parser d'arguments CLI."""
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
        '--open-periods',
        action='store_true',
        dest='open_periods',
        help=(
            'Récupère les périodes d\'ouverture de la semaine courante et '
            'génère la heatmap de taux d\'occupation (créneaux fermés grisés).'
        ),
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
    return parser


def _run_analyses(
        args: argparse.Namespace,
        df_all: pd.DataFrame,
        df_conf: pd.DataFrame,
        period_label: str,
        open_count: pd.DataFrame | None = None,
) -> None:
    """Génère les heatmaps et analyses optionnelles (gaps, trend, score, simulate).

    Args:
        args: Arguments CLI parsés.
        df_all: DataFrame complet (confirmés + annulés).
        df_conf: DataFrame des RDVs confirmés uniquement.
        period_label: Libellé de la période pour les titres de graphiques.
        open_count: DataFrame (slot × weekday) issu de _open_count_matrix,
            ou None si --open-periods n'est pas activé.
    """
    for rdv_type in args.rdv_types:
        label_suffix, prefix = _RDV_TYPES[rdv_type]
        subset = _filter_by_type(df_all, rdv_type)
        _generate_pair(
            subset, label_suffix + period_label, prefix,
            args.output, args.resolution, open_count,
        )

    if args.gaps:
        plot_gap_heatmap(
            df_conf, period_label,
            os.path.join(args.output, 'heatmap_gaps.png'),
            args.resolution, open_count,
        )
    if args.trend:
        plot_trend(df_all, period_label, os.path.join(args.output, 'trend.png'))
    if args.score:
        plot_score_heatmap(
            df_all, period_label,
            os.path.join(args.output, 'heatmap_score.png'),
            args.resolution, args.weeks, open_count,
        )
    if args.simulate:
        weekday = _parse_weekday(args.simulate[0])
        slot = _parse_time_to_slot(args.simulate[1], args.resolution)
        day_str = _DAYS_FR[weekday]
        time_str = args.simulate[1].replace(':', 'h')
        simulate_slot(
            df_conf,
            (weekday, slot),
            (args.weeks, args.simulate_weeks),
            os.path.join(args.output, f'simulation_{day_str.lower()}_{time_str}.png'),
            args.resolution,
            open_count,
        )
    if open_count is not None:
        plot_occupancy_heatmap(
            df_conf, open_count,
            period_label,
            os.path.join(args.output, 'heatmap_occupation.png'),
            args.resolution,
        )


def _run_forecast(
        args: argparse.Namespace,
        df_all: pd.DataFrame,
        df_conf: pd.DataFrame,
        cache_path: str | None,
        config: dict[str, Any],
        cookies: Any,
) -> None:
    """Génère les analyses prévisionnelles depuis les semaines futures du cache.

    Les périodes d'ouverture des semaines futures sont toujours récupérées
    sans cache (elles peuvent changer suite à des fermetures ponctuelles).

    Args:
        args: Arguments CLI parsés.
        df_all: DataFrame historique complet (confirmés + annulés).
        df_conf: DataFrame historique des RDVs confirmés uniquement.
        cache_path: Chemin du cache JSON RDVs, ou None si cache désactivé.
        config: Configuration globale du script.
        cookies: Cookies d'authentification Doctolib.
    """
    print(
        f'\n{_ANSI_BLUE}Analyses prévisionnelles '
        f'({args.forecast_weeks} semaine(s) futures)...{_ANSI_RESET}'
    )
    future_rdvs, future_weeks = load_future_from_cache(cache_path, args.forecast_weeks)
    if not future_rdvs:
        print(
            f'  {_ANSI_RED}Aucune donnée future dans le cache. '
            f'Lancez doctosync.py pour alimenter le cache.{_ANSI_RESET}'
        )
        return

    print(f'  Semaines chargées : {", ".join(future_weeks)}')
    df_future = _parse_appointments(future_rdvs, args.resolution)
    cancel_rate = _cancel_rate_matrix(df_all)
    hist_avg = len(df_conf) / args.weeks

    # Périodes d'ouverture futures : fetch sans cache (données volatiles).
    open_slots_by_week: dict[str, int] | None = None
    print(
        f'  {_ANSI_BLUE}Récupération des périodes d\'ouverture futures '
        f'(sans cache)...{_ANSI_RESET}'
    )
    open_slots_by_week = {}
    for ws in future_weeks:
        try:
            events = fetch_recurring_events(config, ws, cookies)
            periods = _parse_open_periods(events, args.resolution)
            open_slots_by_week[ws] = sum(len(s) for s in periods.values())
        except requests.HTTPError as e:
            print(
                f'    {_ANSI_RED}Erreur périodes {ws}: {e}{_ANSI_RESET}'
            )
            if e.response is not None and e.response.status_code == 401:
                print(
                    f'    {_ANSI_RED}Session expirée (401) — '
                    f'périodes d\'ouverture ignorées.{_ANSI_RESET}'
                )
                open_slots_by_week = None
                break
            open_slots_by_week[ws] = 0
        except requests.RequestException as e:
            print(
                f'    {_ANSI_RED}Erreur périodes {ws}: {e}{_ANSI_RESET}'
            )
            open_slots_by_week[ws] = 0
    if open_slots_by_week is not None and not any(open_slots_by_week.values()):
        open_slots_by_week = None

    plot_fill_forecast(
        df_future, hist_avg,
        os.path.join(args.output, 'forecast_remplissage.png'),
        open_slots_by_week,
    )
    plot_cancel_risk_forecast(
        df_future, cancel_rate,
        os.path.join(args.output, 'forecast_risque_annulation.png'), args.resolution,
    )
    plot_charge_glissante(
        df_all, df_future, os.path.join(args.output, 'forecast_charge_glissante.png')
    )
    plot_carnet_projection(
        df_future, cancel_rate, os.path.join(args.output, 'forecast_carnet_projection.png')
    )


def main() -> None:
    """Point d'entrée principal du script d'analyse heatmap."""
    args = _build_arg_parser().parse_args()

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

    open_count = None
    if args.open_periods:
        open_cache_path = None
        if cache_path:
            cache_dir = os.path.dirname(cache_path) or 'cache'
            open_cache_path = os.path.join(
                cache_dir, '.open_periods_cache.json'
            )
        print(
            f'\n{_ANSI_BLUE}Récupération des périodes d\'ouverture '
            f'({args.weeks} semaines)...{_ANSI_RESET}'
        )
        events_by_week = fetch_all_open_periods(
            config, cookies, week_starts, open_cache_path
        )
        open_count = _open_count_matrix(events_by_week, args.resolution)
        if not open_count.empty:
            n_open = int((open_count > 0).values.sum())
            print(
                f'  {n_open} créneaux-semaines ouverts '
                f'sur {len(events_by_week)} semaine(s).'
            )

    _run_analyses(args, df_all, df_conf, period_label, open_count)

    if args.forecast:
        _run_forecast(args, df_all, df_conf, cache_path, config, cookies)


if __name__ == '__main__':
    main()
