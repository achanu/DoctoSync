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
