# Disponibilité affichée des offres

Les listes et les fiches distinguent **Ouverture confirmée** et **À vérifier**.
La confirmation décrit ce que l'agent a pu constater à la date indiquée ; elle
ne garantit pas qu'une annonce restera ouverte après ce contrôle.

## Ouverture confirmée

Au moins une source actuellement activée pour cette annonce possède une preuve
d'ouverture datant de moins de six heures. Le dernier résultat de cette source
doit être `OPEN`. L'identité de l'annonce doit correspondre à l'observation
stockée : annonce, source et URL exacte.

La confirmation doit aussi correspondre à l'URL originale affichée. Une preuve
sur une autre source ne suffit pas à confirmer ce lien. Les liens descriptifs
déjà choisis sont conservés ; un lien transformé sans preuve directe reste
**À vérifier**.

Une ancienne confirmation suivie d'un contrôle `UNKNOWN` ne suffit pas. Une
simple collecte récente, un email LinkedIn, une page HTTP 200 ou un formulaire
visible ne constituent pas automatiquement une preuve d'ouverture.

## À vérifier

Ce statut apparaît lorsque la confirmation manque, est trop ancienne ou n'a pas
pu être renouvelée. Une demande de connexion, une restriction d'accès, un refus
robots ou une panne réseau ne prouvent pas la fermeture d'une annonce.

Une date absente, incorrecte ou située dans le futur ne permet pas de présenter
l'ouverture comme confirmée. Un fichier de suivi indisponible ou invalide laisse
les offres consultables avec le statut **À vérifier**.

## Les dates

- **Ouverture confirmée le…** : date de la preuve positive retenue.
- **Dernier contrôle…** : date du dernier contrôle de disponibilité enregistré
  pour une source encore liée à cette annonce. Ce repère est l'historique global
  de l'annonce, même si son lien original a changé.
- **Dernière détection par Radar** : passage de collecte. Cette date est
  indépendante des deux précédentes.

Les contrôles historiques ne déclenchent aucune requête vers les sites sources
pendant l'ouverture d'une page utilisateur. L'API lit les résultats existants et
ne modifie ni les scores, ni les candidatures, ni les historiques d'analyse.

## Filtrer les offres

Dans **Offres**, cocher **Voir seulement les offres vérifiées**, puis appliquer
les filtres. La sélection se combine avec Maroc, la ville, le type d'offre, le
domaine, la source et la période de collecte.

Le total et la pagination portent sur les offres qui satisfont tous ces filtres.
La case reste active lors d'un changement de page ; **Réinitialiser** rétablit
l'ensemble du catalogue Data/IA au Maroc.

Les offres dont la fermeture a été confirmée continuent d'être masquées par le
mécanisme existant. Aucun historique de candidature n'est supprimé. Cette
évolution ajoute l'explication visible des contrôles déjà présents ; elle ne
transforme pas une disponibilité inconnue en fermeture.
