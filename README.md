# DoctoSync

Scripts Python pour exploiter les données de rendez-vous Doctolib.

| Script | Rôle |
|--------|------|
| `doctosync.py` | Synchronise les RDVs Doctolib → Google Calendar |
| `docto_heatmap.py` | Génère des heatmaps d'analyse sur les semaines passées |

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

Synchronise les RDVs de la semaine courante (et des semaines suivantes) vers un calendrier Google.

### Utilisation

```bash
python doctosync.py          # semaine courante uniquement
python doctosync.py -w 2     # semaine courante + semaine suivante
```

### Options

| Option | Défaut | Description |
|--------|--------|-------------|
| `-w N` / `--weeks N` | `1` | Nombre de semaines à synchroniser (à partir de la semaine courante) |

### Comportement

- **Création** : les RDVs présents dans Doctolib mais absents du calendrier sont créés.
- **Mise à jour** : si la localisation ou les rappels ont changé dans la config, l'événement est mis à jour.
- **Suppression** : les événements du calendrier qui n'existent plus dans Doctolib sont supprimés.
- **Ignorés** : les RDVs avec le statut `deleted` ou `no_show_but_ok` sont exclus.

### Première exécution

Au premier lancement, une fenêtre de navigateur s'ouvre pour l'authentification Google OAuth. Le token est ensuite sauvegardé dans `config/token.json`.

---

## `docto_heatmap.py` — Heatmaps d'analyse

Récupère les RDVs des N dernières semaines et génère deux heatmaps PNG :

- **Mensuelle** : fréquence par jour du mois (1–31), agrégée sur la période.
- **Hebdomadaire** : fréquence par créneau horaire × jour de semaine, en tenant compte de la durée complète de chaque RDV.

### Utilisation

```bash
# Vue agrégée, 12 semaines passées (défaut)
python docto_heatmap.py

# 8 semaines, créneaux d'1 heure, sans cache
python docto_heatmap.py -w 8 -r 60 --no-cache

# Vues séparées nouveaux patients et suivis
python docto_heatmap.py --type new followup

# Toutes les vues en une seule commande
python docto_heatmap.py --type all new followup
```

### Options

| Option | Défaut | Description |
|--------|--------|-------------|
| `-w N` / `--weeks N` | `12` | Nombre de semaines passées à analyser |
| `-r N` / `--resolution N` | `30` | Résolution des créneaux en minutes (diviseur de 60 : 15, 20, 30, 60) |
| `-o DIR` / `--output DIR` | `output/` | Répertoire de sortie des PNG |
| `-c PATH` / `--config PATH` | `config/config.yaml` | Chemin vers la configuration |
| `--type TYPE...` | `all` | Types de RDVs : `all`, `new`, `followup` (multi-valeur) |
| `--cache-file PATH` | `cache/.heatmap_cache.json` | Chemin du fichier de cache |
| `--no-cache` | — | Désactive le cache (re-requête toutes les semaines) |

### Cache

Les données des semaines passées ne changent pas. Elles sont mises en cache dans `cache/.heatmap_cache.json` pour éviter des appels répétés à l'API. Le cache est mis à jour automatiquement lors de nouvelles semaines découvertes.

### Fichiers générés

| `--type` | Fichiers produits |
|----------|-------------------|
| `all` | `heatmap_monthly_all.png`, `heatmap_weekly_all.png` |
| `new` | `heatmap_monthly_new.png`, `heatmap_weekly_new.png` |
| `followup` | `heatmap_monthly_followup.png`, `heatmap_weekly_followup.png` |

### Durée des RDVs dans la heatmap hebdomadaire

Un RDV de 09h00 à 10h30 avec une résolution de 30 min est comptabilisé dans les créneaux **09h00**, **09h30** et **10h00**. Cela reflète l'occupation réelle du praticien plutôt que la seule heure de début.

---

## Structure du projet

```
DoctoSync/
├── doctosync.py          # Script de synchronisation
├── docto_heatmap.py      # Script d'analyse heatmap
├── requirements.txt
├── config/
│   ├── config.yaml.example
│   ├── config.yaml       # (ignoré par git)
│   ├── cookies.txt       # (ignoré par git)
│   ├── credentials.json  # (ignoré par git)
│   └── token.json        # (ignoré par git)
├── cache/                # Cache des données Doctolib (ignoré par git)
└── output/               # Heatmaps générées (ignoré par git)
```
