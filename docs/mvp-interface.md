# Opportunity Radar AI — démarrage

Le projet se trouve dans `C:\Users\pc\Documents\OPPORTUNITY-RADAR-AI-TRANSFER`.

## Utilisation

Dans le dossier du projet :

1. Double-cliquer sur **Lancer-Radar.cmd**. Une fenêtre indique la progression du démarrage.
2. Attendre la préparation du site et de l’API : le navigateur s’ouvre automatiquement quand ils sont prêts.
3. Si une erreur apparaît, la fenêtre reste ouverte pour permettre de lire le message. Les détails restent dans `data\runtime`.

**Ouvrir Radar.url** ouvre seulement l’adresse : il faut que le serveur soit déjà démarré. Le lancement automatique à l’ouverture de session Windows reste discret, sans fenêtre ni ouverture du navigateur.

L’aperçu de l’agent Codex est limité par son isolation Windows : utiliser le lancement normal ci-dessus pour accéder aux données locales.

Le menu à gauche donne accès à l’accueil, aux offres, aux recommandations, aux candidatures et aux sources. Les filtres agissent sur les offres réellement enregistrées. « Sauvegarder », « Préparer la candidature » et « J’ai postulé » mettent à jour le suivi local.

### Consulter l’annonce originale

**Voir l’offre originale** ouvre directement l’annonce externe dans un nouvel onglet. Le clic ne passe pas par une fiche de description dans Radar.

Pour Artefact, le lien de description est associé au même poste par son identifiant interne Greenhouse, son titre et son lieu. Le collecteur actualise cette correspondance depuis les données publiques ; le lien d’origine reste disponible lorsqu’aucune correspondance n’est vérifiée.

Le formulaire de candidature du site externe peut ensuite demander les coordonnées et le CV. Ouvrir l’annonce ou le formulaire n’envoie aucune candidature automatiquement.

### Sélection Maroc

L’accueil présente un aperçu des offres Data & IA avec une localisation identifiée au Maroc. Le compteur et les cartes se mettent à jour avec les collectes et les contrôles de disponibilité ; les offres confirmées fermées ne sont plus comptées.

La page **Offres** sélectionne **Maroc** par défaut ; les villes proposées correspondent au pays actif. Les raccourcis **Toutes les offres**, **Stages**, **PFE** et **Emplois** de l’accueil gardent ce pays. « Réinitialiser » revient à la sélection Maroc.

La sélection couvre tous les niveaux d’expérience en Data & IA. La catégorie **Emplois — tous niveaux** utilise le type d’annonce déjà enregistré : elle inclut les postes juniors, expérimentés et de management. Les stages PFA/PFE, les stages d’été et de pré-embauche sont regroupés dans **Stages** ; PFE et Alternance restent accessibles séparément.

Le contrat des emplois présents n’est pas renseigné dans les données collectées. Les CDI et CDD peuvent être consultés avec les autres emplois ; vérifier le contrat exact sur l’annonce d’origine. Une offre sans contrat précisé conserve l’étiquette **Emploi**.

Pour élargir volontairement la recherche, choisir **Tous les pays** dans le filtre de pays. Les pages suivantes conservent ce choix. Les recommandations utilisent le profil existant, dont la mobilité est déjà limitée au Maroc.

Le bandeau, les chiffres et les dates utilisent les données du radar. La photo est une illustration éditoriale créée pour cette interface. Une date de collecte ne prouve pas que l’annonce accepte encore les candidatures.

### Offres expirées

L’agent contrôle la disponibilité après chaque collecte, avant le recalcul des recommandations. Une fermeture confirmée masque l’annonce dans l’accueil, les offres, les correspondances, les recommandations, les priorités et le portefeuille. Les candidatures déjà enregistrées et leur historique sont conservés.

Une preuve de fermeture peut être le retrait du poste d’un catalogue Greenhouse complet, une réponse 404/410 sur l’annonce exacte, un message explicite de fermeture ou une date `validThrough` dépassée liée à cette annonce. La seule ancienneté d’une offre ne suffit pas. Une nouvelle collecte, y compris un ancien email LinkedIn, ne réactive plus une offre expirée ; une preuve récente d’ouverture est nécessaire.

Les catalogues Greenhouse sont vérifiés à chaque cycle. Pour les autres sources, l’agent contrôle jusqu’à 50 pages par cycle, en commençant par les contrôles les plus anciens, puis revérifie après six heures. Les vérifications restantes passent dans les cycles suivants.

Une panne réseau, une demande de connexion, une restriction robots ou un blocage LinkedIn laisse la disponibilité **non vérifiable**. L’agent ne déclare pas l’offre fermée sans preuve. Le cycle indique alors `PARTIAL` ; les contrôles et les collectes accessibles continuent.

Le résumé se trouve dans `data\runtime\availability-state.json` ; les motifs des contrôles sont conservés dans `availability-events.jsonl`. Ces fichiers ne contiennent pas le texte complet des pages ni de coordonnées personnelles.

## Démarrage automatique

Le raccourci **Opportunity Radar AI** du dossier Démarrage Windows lance le site et la collecte à l’ouverture de session. Un cycle commence immédiatement, puis le suivant 60 minutes après la fin du précédent. Le PC doit rester allumé et connecté pour collecter.

La collecte contrôle les expirations puis recalcule les correspondances, recommandations, priorités et portefeuille avec le profil existant. Une autorisation Gmail indisponible fait échouer cette source sans ouvrir de fenêtre de connexion. Les sources publiques continuent.

## Lancement et arrêt manuels

Depuis le dossier du projet, dans PowerShell :

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-radar.ps1
```

Pour arrêter le radar :

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-radar.ps1
```

Pour retirer le démarrage automatique :

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-startup.ps1 -Remove
```

Le diagnostic système est disponible dans **Sources → État du système**. Les journaux du lancement et du recalcul restent dans `data\runtime`. Une sauvegarde de la base précédant la remise en route est conservée dans `data\backups`.

## Données après la remise en route

La première collecte publique a ajouté **98 offres**, pour **517 enregistrements** avant le nettoyage des expirations. Les annonces fermées restent archivées dans la base mais disparaissent des listes publiques. Les compteurs du site reflètent uniquement les offres encore actives.

Le contrôle du 10 octobre 2026 a masqué **98 annonces fermées** : 25 Artefact, 72 Scale AI et l’offre Devoteam signalée par l’utilisateur. **419 annonces restent actives**, dont **21 offres Data & IA localisées au Maroc**. Les quatre classements ont été recalculés avec succès. Les 51 autres liens LinkedIn n’ont pas pu être vérifiés publiquement ; ils ne sont pas déclarés expirés sans preuve.

Les quatre sources publiques ont répondu correctement. Les alertes LinkedIn via Gmail signalent `GmailAuthenticationError` : l’autorisation Google doit être renouvelée pour reprendre cette source. Les autres collectes et le recalcul continuent. La procédure OAuth existante est décrite dans `docs\operations.md`, section Gmail ; la reconnexion n’a pas été déclenchée automatiquement.

L’interface a été compilée avec succès. Les interactions dans le navigateur n’ont pas pu être vérifiées depuis l’environnement isolé de l’agent.
