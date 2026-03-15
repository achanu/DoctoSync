# TODO — Évolutions futures

## Taux de remplissage réel

**Objectif** : comparer les RDVs pris avec les créneaux *ouverts* sur Doctolib,
pour obtenir un vrai taux de remplissage (et non juste un comptage de RDVs).

**Bloquant** : nécessite de fetcher les créneaux disponibles via l'API
(endpoint de disponibilités), en plus des RDVs pris. À investiguer.

**Lien** : similaire à "Créneaux manqués" ci-dessous — les deux partagent le
même prérequis de données sur les créneaux ouverts.

---

## Créneaux manqués (slots ouverts non réservés)

**Objectif** : identifier les créneaux régulièrement ouverts mais jamais
(ou rarement) réservés — ceux à supprimer ou déplacer.

**Bloquant** : même prérequis que le taux de remplissage (données de
disponibilités depuis l'API).

---

## Détection des annulations tardives

**Objectif** : identifier les créneaux ou jours ayant un taux élevé
d'annulations à J-1 ou J-2, pour permettre une reconfirmation active ou
une réorganisation préventive du planning.

**Implémentation** :
  - Calculer le `lead_time_days` des RDVs annulés (déjà disponible dans le cache).
  - Filtrer les annulations à `lead_time_days ≤ 2`.
  - Agréger par (weekday, start_slot) pour détecter les zones à risque.
  - Produire une heatmap ou un tableau de bord des créneaux à surveiller.

---

## Alerte créneau vide dans l'agenda futur

**Objectif** : détecter automatiquement les trous dans le planning futur
(plages habituellement occupées d'après l'historique mais libres cette semaine)
et les signaler en sortie console ou dans un rapport.

**Implémentation** :
  - Croiser la heatmap historique (`_weekly_matrix`) avec les créneaux
    effectivement réservés dans le cache futur.
  - Un créneau est "anormal" si son score historique est élevé (> seuil)
    mais absent du planning futur.
  - Sortie : liste console `[Jour HH:MM] créneau attendu non réservé`.

---

## Analyse de fidélité patients

**Objectif** : mesurer la fréquence de retour des patients (combien reviennent
toutes les X semaines, quels créneaux sont "réservés" par des habitués).

**Contrainte forte** : les données de santé sont confidentielles. Avant toute
implémentation, définir une stratégie d'anonymisation :
  - Hachage des identifiants patients (SHA-256 + sel local, non réversible).
  - Agrégation en cohortes (pas d'analyse individuelle).
  - Stockage séparé des données anonymisées (hors du cache principal).
  - Validation RGPD de l'approche retenue.

**Note** : cette fonctionnalité ne sera pas implémentée de façon automatique.
Le praticien agit en intermédiaire pour fournir les données agrégées nécessaires.
