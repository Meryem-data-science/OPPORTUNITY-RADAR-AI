# Connecter les alertes LinkedIn et Indeed

Le projet récupère les annonces contenues dans les **alertes LinkedIn et Indeed reçues par Gmail**. La collecte porte sur les emails reçus, sans importer la totalité des catalogues de ces plateformes. Stage.ma et Stagiaires.ma utilisent leurs collecteurs publics existants et n'ont pas besoin de cette connexion.

## Reconnecter une configuration existante

1. Dans le dossier du projet, double-cliquer sur **Connecter-Alertes-Emploi.cmd**. L'ancien **Connecter-Alertes-LinkedIn.cmd** reste compatible et utilise la même autorisation.
2. Dans le navigateur Google qui s'ouvre, choisir le compte Gmail qui reçoit les alertes et autoriser la **lecture seule**.
3. Attendre le message **« Autorisation Gmail en lecture seule enregistrée localement »** dans la fenêtre du lanceur. La page de retour dans le navigateur ne confirme pas à elle seule l'enregistrement du jeton.

Le lanceur demande un nouveau consentement même si l'ancien jeton est expiré ou révoqué. Il conserve l'ancien fichier jusqu'à la réception et la validation d'une autorisation renouvelable avec le seul périmètre `gmail.readonly`. Il remplace ensuite le jeton par une écriture atomique. Aucun email n'est lu ou envoyé par ce lanceur.

La collecte reprendra au **prochain cycle automatique** de l'agent si celui-ci est lancé. Le lanceur ne collecte pas les mails et ne redémarre pas le site. Utiliser `Lancer-Radar.cmd` si l'agent est arrêté. La source ne sera considérée opérationnelle qu'après une collecte réussie ; le renouvellement OAuth seul ne confirme pas la présence d'annonces.

Une seule autorisation Gmail en lecture seule sert aux deux sources. Aucun second jeton Indeed n'est nécessaire lorsque la connexion actuelle est valide.

## Sans domaine acheté ni carte bancaire pour ce collecteur local

La méthode utilise un client Google OAuth de type **Application de bureau**. Le retour de connexion est limité à `127.0.0.1` sur un port local temporaire : **aucun domaine Internet acheté n'est nécessaire pour cette redirection**. [Flux officiel Google pour applications installées](https://developers.google.com/identity/protocols/oauth2/native-app#redirect-uri_loopback).

Le parcours officiel Gmail demande un projet Google Cloud et un compte Gmail, puis l'activation de l'API et la création d'un client OAuth Desktop. La saisie d'une carte bancaire et l'activation d'un essai payant ne sont pas des étapes de ce parcours. [Démarrage officiel Gmail](https://developers.google.com/workspace/gmail/api/quickstart/python).

Cette configuration vise l'agent installé sur le PC pour l'usage personnel. Une application distribuée au public peut avoir des exigences de vérification et de domaine supplémentaires. L'usage standard de Gmail API est sans coût supplémentaire ; ne pas confondre cela avec une garantie d'usage illimité ou de gratuité de tous les services Google Cloud. [Politiques OAuth](https://developers.google.com/identity/protocols/oauth2/policies), [Quotas et tarification Gmail](https://developers.google.com/workspace/gmail/api/reference/quota).

## Si la configuration manque ou si le client est de type Web

1. Ouvrir [Google Cloud Console](https://console.cloud.google.com/) avec le compte Google concerné, puis sélectionner ou créer un projet. L'agent ne demande pas d'activer la facturation, un hébergement Cloud ou un essai payant.
2. Activer **Gmail API** dans « API et services ».
3. Configurer Google Auth Platform avec un nom d'application et une adresse de contact. Pour un compte Gmail personnel, choisir l'audience **External** et ajouter ce même compte aux utilisateurs de test si l'application est en mode **Testing**. L'audience **Internal** concerne une organisation Google Workspace.
4. Dans « Clients », créer un client OAuth de type **Application de bureau**, puis télécharger son JSON. Le lanceur refuse un client de type « Application Web » et n'accepte que les endpoints OAuth officiels Google.
5. Conserver ce fichier dans `.secrets/`, un dossier ignoré par Git. Par exemple : `.secrets/gmail-oauth-client.json`. Ne pas écraser un autre fichier d'autorisation existant sans connaître son usage.
6. Dans la configuration locale `.env`, renseigner les chemins `GMAIL_OAUTH_CLIENT_SECRET_PATH` et `GMAIL_TOKEN_PATH`. Le fichier du client et le jeton doivent être **distincts**. Exemple pour une nouvelle configuration :

   ```dotenv
   GMAIL_OAUTH_CLIENT_SECRET_PATH=.secrets/gmail-oauth-client.json
   GMAIL_TOKEN_PATH=.secrets/linkedin-readonly-token.json
   ```

   Une configuration existante valide doit garder ses chemins actuels pour que l'agent et le lanceur utilisent le même jeton. Ne pas réutiliser le jeton du digest Gmail, qui nécessite `gmail.send`.

7. Double-cliquer sur **Connecter-Alertes-Emploi.cmd** et terminer le consentement dans Google. Si le navigateur ne s'ouvre pas automatiquement, le lanceur affiche un lien d'autorisation. L'attente est limitée à cinq minutes ; relancer en cas d'expiration.

Une application External en mode Testing utilisant Gmail impose une nouvelle autorisation après sept jours. Pour un usage personnel durable, examiner le passage en production et les exemptions de vérification ; ce changement ne garantit pas un jeton permanent. [Audience et durée d'autorisation](https://support.google.com/cloud/answer/15549945?hl=fr), [Exceptions de vérification](https://support.google.com/cloud/answer/13464323?hl=fr).

Ne jamais envoyer les fichiers client, jetons ou mots de passe dans le chat ou sur GitHub. L'agent de développement ne les consulte pas. Le lanceur les utilise uniquement localement dans la session Windows personnelle.

## Préparer les annonces souhaitées

Dans LinkedIn, créer des alertes pour **Maroc** et les métiers Data/IA, par exemple Data Analyst, Data Engineer, Data Scientist et Machine Learning. Inclure les stages/PFE et les emplois souhaités, puis activer la réception par email sur le compte Gmail autorisé. [Alertes emploi LinkedIn](https://www.linkedin.com/help/linkedin/answer/a508396).

Si les offres Indeed arrivent déjà dans le compte Gmail autorisé, **aucune nouvelle activation n'est nécessaire**. Pour créer d'autres recherches, choisir la localisation **Maroc** ou une ville marocaine et activer leur réception par email sur ce compte. Voir le [guide officiel des alertes Indeed](https://www.indeed.com/help/job-seekers/articles/204488890-starting-stopping-and-managing-job-alerts?co=US&hl=en).

Chaque source lit au maximum **50 messages des sept derniers jours**, puis réutilise la déduplication, la qualification Data/IA et les filtres Maroc du site. LinkedIn recherche les messages de `jobalerts-noreply@linkedin.com` ; Indeed recherche ceux de `alert@indeed.com`, `donotreply@jobalert.indeed.com` et `donotreply@match.indeed.com`, selon `config/sources.yaml`. L'alerte doit arriver dans le compte Gmail autorisé par Radar, qui peut être différent de l'adresse utilisée pour se connecter à Indeed.

Les informations importées dépendent du contenu réel des emails. Une localisation absente ou ambiguë ne devient pas automatiquement « Maroc » et l'annonce ne paraît alors pas dans la sélection Maroc. Un email Indeed non reconnu est écarté en entier ; les autres emails entièrement reconnus peuvent être importés. La page **Sources** conserve alors un diagnostic de collecte partielle en échec et les compteurs réels. Si aucun email ne peut être extrait, la source échoue sans nouvelle offre. Les liens dont la destination peut être lue localement sont décodés sans ouvrir le suivi ; les redirections opaques restent non prises en charge.

Une alerte ne garantit pas que l'annonce accepte encore des candidatures. Un ancien email ne réactive pas une offre confirmée fermée et la disponibilité reste à vérifier en l'absence de preuve récente.

Si les emails Indeed sont déjà reçus mais que la collecte reste vide, vérifier le compte Gmail autorisé, leur date et leur adresse d'expéditeur avant de modifier les alertes. Les messages de plus de sept jours ne sont pas relus par la collecte courante. Un expéditeur différent ou un format d'email non reconnu demande une adaptation du collecteur ; réactiver les alertes ne corrige pas ce cas.

## Si Google ou le lanceur refuse la connexion

- **Fichier absent ou configuration incomplète** : suivre les étapes ci-dessus. Ne pas acheter un domaine pour corriger ce message.
- **Client Application Web** : créer un client Application de bureau ; aucune nouvelle clé API payante n'est requise.
- **Accès refusé ou utilisateur non autorisé** : vérifier le compte choisi, l'audience et les utilisateurs de test dans Google Auth Platform. Le lanceur ne contourne pas un refus de Google.
- **Délai dépassé ou connexion interrompue** : relancer et terminer la connexion dans les cinq minutes.
- **Dépendances manquantes** : installer les dépendances déclarées dans `pyproject.toml` dans `.venv`, comme pour le lancement du projet.

Le lanceur affiche uniquement des messages fixes ou le type d'une erreur fournisseur, jamais le contenu des secrets. Il n'est pas appelé par la tâche automatique Windows : celle-ci utilise seulement une autorisation déjà enregistrée.
