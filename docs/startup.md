# Radar local : lancement et actualisation

Le lancement utilise le profil existant. L'import CV ne fait pas partie de ce
parcours. La collecte alimente les offres puis reconstruit, dans cet ordre, leurs
contraintes, exigences, lieux, eligibilite, Matching, Recommendation, Priority et
Portfolio. Les poids et les decisions restent ceux des services existants.

## Prerequis

- Python 3.12 avec `pip install -e .` dans `.venv`.
- Node.js, dependances de `apps/web` installees et `next build` deja termine.
- La base SQLite existante, migree jusqu'a `0026` au minimum, et un profil deja
  present. Les scripts n'appliquent pas de migrations et ne creent pas de profil.
- Dans `.env` a la racine : `DATABASE_BACKEND=sqlite`,
  `SQLITE_DATABASE_PATH=data/opportunity-radar.db` et
  `OPPORTUNITY_RADAR_PROFILE_ID` avec l'identifiant du profil existant.
- Facultatif : `OPPORTUNITY_RADAR_TIMEZONE=Europe/Paris`, `RADAR_PYTHON` et
  `RADAR_NODE` pour imposer les chemins des executables.

Les scripts chargent `.env` comme des donnees. Une variable deja definie pour le
processus garde la priorite. Les chemins SQLite relatifs sont resolves depuis la
racine du projet. Le Python de `.venv` est privilegie; Node.js peut aussi etre
trouve dans le runtime local Codex. Aucun appel a npm n'est necessaire au lancement.

## Ouverture depuis Windows

Double-cliquer sur `Lancer-Radar.cmd` a la racine du projet. Le lanceur affiche
la progression, demarre le superviseur en arriere-plan et attend que l'API et
l'interface repondent avant d'ouvrir le navigateur. Cette attente peut durer
jusqu'a 180 secondes. En cas d'echec, la fenetre reste ouverte avec le message.

`Ouvrir Radar.url` ouvre seulement l'adresse : il ne demarre pas les services.
Le lancement automatique a l'ouverture de session reste cache et n'ouvre pas
le navigateur.

## Commandes PowerShell

Depuis la racine du projet :

```powershell
# Preflight de la configuration et du schema, sans collecte ni ecriture en base.
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\collect-radar.ps1 -Check

# Un cycle de collecte et de recalcul.
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\collect-radar.ps1

# Recalculer les offres deja presentes, sans appel aux sources.
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\collect-radar.ps1 -SkipCollection

# API, interface et collecte immediate, puis toutes les 60 minutes.
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-radar.ps1

# Installer le lancement cache a l'ouverture de session du compte courant.
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-startup.ps1

# Arreter les processus lances par le superviseur.
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-radar.ps1

# Supprimer le raccourci de demarrage automatique.
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-startup.ps1 -Remove
```

L'interface est disponible sur `http://127.0.0.1:3000` et l'API sur
`http://127.0.0.1:8000`. Elles ecouteront uniquement sur cet ordinateur.
`-IntervalMinutes 120` change la frequence. `-ApiPort` et `-WebPort` changent les
ports du lancement manuel. Le raccourci Windows utilise les ports par defaut.

Le raccourci `Opportunity Radar AI.lnk` est installe dans le dossier Demarrage du
compte Windows courant, sans registre ni droits administrateur. Il s'execute a
l'ouverture de session et suppose que l'ordinateur reste allume. Il ne lance pas
le projet immediatement lors de son installation.

## Etat et incidents

Le fichier `data/runtime/refresh-status.json` donne les etapes, leurs compteurs
et les erreurs par type. `refresh.log` est un journal JSON avec rotation
(2 Mo, cinq archives). `supervisor.log`, les fichiers `api-*.log`, `web-*.log` et
`refresh-*.log` decrivent les lancements; ces derniers restent sur le disque.
`bootstrap.log` capture les erreurs avant le chargement de la configuration.
Les fichiers `manual-launch-*.log` et `supervisor-*-errors.log` permettent de
retrouver un echec du lanceur manuel.

Deux cycles ne peuvent pas se chevaucher sur la meme base : un verrou du systeme
est libere automatiquement si le processus se termine. Un seul superviseur peut
fonctionner dans le dossier du projet. Il relance ses processus apres un arret
inattendu, avec un delai de 60 secondes entre tentatives. Un port deja occupe par
un autre lancement provoque un refus, sans tuer ce processus.

Une source en echec est indiquee `PARTIAL`, mais les autres sources et le recalcul
sur les offres disponibles continuent. Une etape interne en echec empeche ses
etapes dependantes d'utiliser une projection qu'elle n'a pas preparee. Les
transactions existantes protegent chaque etape, sans transaction globale longue
pendant les acces reseau. Codes du cycle ponctuel : `0` termine, `2` partiel,
`1` erreur de configuration ou d'execution.

Gmail utilise seulement une autorisation `gmail.readonly` deja presente :
`GMAIL_OAUTH_CLIENT_SECRET_PATH` et `GMAIL_TOKEN_PATH` restent ceux de `.env`.
Le rafraichissement OAuth est fait en memoire; aucun consentement navigateur ne
s'ouvre et le fichier token n'est pas modifie. Une autorisation absente ou invalide
fait echouer uniquement la source Gmail. Les autres collecteurs publics restent
disponibles. Ce lancement n'envoie aucun email ni notification et ne change aucun
fait du profil ni candidature.
