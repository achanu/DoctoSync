# DoctoSync

Scripts Python pour exploiter les données de rendez-vous Doctolib.

| Script | Rôle |
|--------|------|
| `doctosync.py` | Synchronise les RDVs Doctolib → Google Calendar et alimente le cache |
| `docto_heatmap.py` | Analyse les semaines passées et génère des analyses prévisionnelles |
| `cache_utils.py` | Module partagé de gestion du cache (lecture/écriture JSON versionné) |

## Prérequis

Python 3.11+ et les dépendances :

```bash
pip install -r requirements.txt
```

Les dépendances communes aux deux scripts (`PyYAML`, `requests`, `browser_cookie3`) sont installées dans tous les cas. Les dépendances propres à chaque script (Google API / matplotlib) ne sont utiles que si vous utilisez le script correspondant.

## Configuration

Copiez le fichier d'exemple et adaptez-le :

```bash
cp config/config.yaml.example config/config.yaml
```

| Clé | Description |
|-----|-------------|
| `api.url` | URL de l'API Doctolib pro |
| `api.agenda_ids` | ID(s) de votre agenda |
| `api.cookie_path` | Chemin vers le fichier de cookies de secours |
| `api.user_agent` | User-Agent à envoyer |
| `calendar.id` | ID du calendrier Google cible |
| `calendar.credentials_path` | Credentials OAuth Google |
| `calendar.token_path` | Token d'accès Google (généré automatiquement) |
| `config.notification` | Rappel par défaut (minutes) |
| `config.first_of_day` | Rappel pour le 1er RDV de la journée (0 = désactivé) |
| `config.localisation` | Adresse ajoutée aux événements Google Calendar |

### Authentification Doctolib

Les cookies sont détectés automatiquement depuis Chrome, Firefox, Edge, Brave, Chromium ou Safari. En cas d'échec, le fichier `config/cookies.txt` est utilisé (format Netscape ou `clé=valeur`).

---

## `doctosync.py` — Synchronisation Doctolib → Google Calendar

Synchronise les RDVs de la semaine courante (et des semaines suivantes) vers un calendrier Google. **Alimente également le cache partagé** avec les données brutes (RDVs confirmés et annulés) pour les analyses prévisionnelles de `docto_heatmap.py`.

### Utilisation

```bash
python doctosync.py          # semaine courante uniquement
python doctosync.py -w 2     # semaine courante + semaine suivante
python doctosync.py --no-cache  # synchro sans écriture dans le cache
```

### Options

| Option | Défaut | Description |
|--------|--------|-------------|
| `-w N` / `--weeks N` | `1` | Nombre de semaines à synchroniser |
| `--cache-file PATH` | `cache/.heatmap_cache.json` | Chemin du cache partagé |
| `--no-cache` | — | Désactive l'écriture dans le cache après la synchro |

### Comportement

- **Création** : les RDVs présents dans Doctolib mais absents du calendrier sont créés.
- **Mise à jour** : si la localisation ou les rappels ont changé dans la config, l'événement est mis à jour.
- **Suppression** : les événements du calendrier qui n'existent plus dans Doctolib sont supprimés.
- **Ignorés** : les RDVs avec le statut `deleted` ou `no_show_but_ok` sont exclus de la synchro Google (mais conservés dans le cache pour les analyses).
- **Cache** : après chaque synchro, les données brutes (tous statuts) sont écrites dans le cache. Le cache n'est **jamais** consulté pour la synchro — Doctolib reste l'unique source de vérité.

### Première exécution

Au premier lancement, une fenêtre de navigateur s'ouvre pour l'authentification Google OAuth. Le token est ensuite sauvegardé dans `config/token.json`.

---

## `docto_heatmap.py` — Analyses et prévisions

Récupère les RDVs des N dernières semaines et génère des analyses visuelles. En mode `--forecast`, exploite le cache alimenté par `doctosync.py` pour projeter les semaines futures sans appel API.

### Utilisation

```bash
# Analyse standard sur 12 semaines passées (défaut)
python docto_heatmap.py

# 8 semaines, créneaux d'1 heure, sans cache
python docto_heatmap.py -w 8 -r 60 --no-cache

# Vues séparées par type de RDV
python docto_heatmap.py --type new followup

# Analyses complémentaires : gaps, tendance, score d'attractivité
python docto_heatmap.py --gaps --trend --score

# Simulation d'ouverture d'un créneau (projection 4 semaines)
python docto_heatmap.py --simulate lun 09:00 --simulate-weeks 4

# Analyses prévisionnelles sur les 4 prochaines semaines (depuis le cache)
python docto_heatmap.py --forecast --forecast-weeks 4

# Tout en une commande
python docto_heatmap.py -w 12 --type all new --gaps --trend --score --forecast
```

### Options

| Option | Défaut | Description |
|--------|--------|-------------|
| `-w N` / `--weeks N` | `12` | Nombre de semaines passées à analyser |
| `-r N` / `--resolution N` | `30` | Résolution des créneaux en minutes (diviseur de 60 : 15, 20, 30, 60) |
| `-o DIR` / `--output DIR` | `output/` | Répertoire de sortie des PNG |
| `-c PATH` / `--config PATH` | `config/config.yaml` | Chemin vers la configuration |
| `--type TYPE...` | `all` | Types : `all`, `new`, `followup`, `cancelled` (multi-valeur) |
| `--cache-file PATH` | `cache/.heatmap_cache.json` | Chemin du cache partagé |
| `--no-cache` | — | Désactive le cache (re-requête toutes les semaines) |
| `--gaps` | — | Heatmap des créneaux libres entre RDVs consécutifs |
| `--trend` | — | Graphique de tendance du volume par semaine |
| `--score` | — | Heatmap du score composite d'attractivité des créneaux |
| `--simulate JOUR HEURE` | — | Simule l'ouverture d'un créneau (ex: `lun 09:00`) |
| `--simulate-weeks N` | `4` | Semaines futures projetées pour la simulation |
| `--forecast` | — | Active les 4 analyses prévisionnelles (nécessite le cache) |
| `--forecast-weeks N` | `4` | Nombre de semaines futures à charger depuis le cache |

### Cache partagé

Le cache `cache/.heatmap_cache.json` est **alimenté par `doctosync.py`** à chaque synchro et **lu par `docto_heatmap.py`** pour les analyses. Il stocke tous les RDVs (confirmés et annulés) par semaine, avec versioning automatique.

- Les données des semaines passées ne changent pas : elles sont servies depuis le cache sans appel API.
- Les données des semaines futures proviennent du cache peuplé par la dernière synchro `doctosync.py`.
- En cas de changement de format, le numéro de version est incrémenté dans `cache_utils.py` et un re-fetch complet est déclenché.

### Fichiers générés

**Analyses historiques (semaines passées)**

| Option | Fichiers produits |
|--------|-------------------|
| `--type all` | `heatmap_monthly_all.png`, `heatmap_weekly_all.png` |
| `--type new` | `heatmap_monthly_new.png`, `heatmap_weekly_new.png` |
| `--type followup` | `heatmap_monthly_followup.png`, `heatmap_weekly_followup.png` |
| `--type cancelled` | `heatmap_monthly_cancelled.png`, `heatmap_weekly_cancelled.png` |
| `--gaps` | `heatmap_gaps.png` |
| `--trend` | `trend.png` |
| `--score` | `heatmap_score.png` |
| `--simulate lun 09:00` | `simulation_lun_09h00.png` |

**Analyses prévisionnelles (`--forecast`)**

| Fichier | Description |
|---------|-------------|
| `forecast_remplissage.png` | RDVs réservés par semaine future vs moyenne historique |
| `forecast_risque_annulation.png` | Heatmap du risque d'annulation par créneau (taux historique × réservations futures) |
| `forecast_charge_glissante.png` | Vue unifiée passé + semaine courante + futur en un seul graphe |
| `forecast_carnet_projection.png` | Projection Bernoulli des RDVs maintenus avec bande d'incertitude ±1σ |

### Durée des RDVs dans la heatmap hebdomadaire

Un RDV de 09h00 à 10h30 avec une résolution de 30 min est comptabilisé dans les créneaux **09h00**, **09h30** et **10h00**. Cela reflète l'occupation réelle du praticien plutôt que la seule heure de début.

---

## Flux recommandé

```
doctosync.py (synchro quotidienne / hebdomadaire)
    └─ alimente cache/.heatmap_cache.json
           │
           ├─ docto_heatmap.py -w 12          # analyse passé
           └─ docto_heatmap.py --forecast      # prévisions futures
```

---

## Structure du projet

```
DoctoSync/
├── doctosync.py          # Synchronisation Doctolib → Google Calendar
├── docto_heatmap.py      # Analyses heatmaps + prévisions
├── cache_utils.py        # Module partagé : load_cache / save_cache
├── requirements.txt
├── config/
│   ├── config.yaml.example
│   ├── config.yaml       # (ignoré par git)
│   ├── cookies.txt       # (ignoré par git)
│   ├── credentials.json  # (ignoré par git)
│   └── token.json        # (ignoré par git)
├── cache/                # Cache partagé des données Doctolib (ignoré par git)
└── output/               # Analyses PNG générées (ignoré par git)
```
