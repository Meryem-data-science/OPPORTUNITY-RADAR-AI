# Sauvegarder et récupérer Opportunity Radar AI

## Sauvegarde après chaque séance de développement

Le mode choisi le **11 octobre 2026** est la sauvegarde du code après chaque séance de développement avec l'agent. L'instruction est conservée dans `AGENTS.md` à la racine du projet ; l'agent relit les changements, crée un commit et l'envoie sur la branche **`sauvegarde-mvp-2026-10-11`** du dépôt [Meryem-data-science/OPPORTUNITY-RADAR-AI](https://github.com/Meryem-data-science/OPPORTUNITY-RADAR-AI/tree/sauvegarde-mvp-2026-10-11).

`AGENTS.md` fournit des instructions persistantes à Codex lorsqu'il travaille dans le projet. Il faut donc ouvrir ce dossier comme projet, ou demander explicitement à l'agent de lire son `AGENTS.md` depuis un autre chat. [Documentation officielle OpenAI](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

Après une séance, la sauvegarde est confirmée quand le nouveau commit est présent sur cette branche GitHub. En cas de panne réseau ou d'autorisation manquante, l'agent indique que l'envoi reste à faire. Il ne force pas le push et ne fusionne pas la sauvegarde dans une autre branche.

Ce mode ne déclenche aucun programme à la fermeture de Windows ou de l'éditeur, ni à une heure fixée.

## Modifications faites en dehors de l'agent

Dans le dossier du projet, double-cliquer sur **`Sauvegarder-GitHub.cmd`** pour lancer la sauvegarde du code. Le lanceur affiche le résultat et garde les erreurs visibles. Attendre la confirmation d'envoi avant de fermer sa fenêtre.

Ce bouton utilise **Git installé sur le PC** et sa propre authentification GitHub. La connexion du connecteur GitHub dans Codex ne connecte pas automatiquement le bouton. Si Git demande une connexion, utiliser le compte **Meryem-data-science** dans la fenêtre d'authentification ; ne pas inscrire de jeton dans le code du projet.

## Périmètre sauvegardé

GitHub conserve le code de l'interface et des services, les assets et licences, la configuration publique, les migrations, les scripts et la documentation. Le dépôt étant public, les fichiers personnels restent exclus.

Les exclusions conservent seulement sur le PC :

- `data/` : base des offres, profil, candidatures, historique et journaux ;
- `.env`, `.secrets/` et les jetons : paramètres privés et autorisations de connexion ;
- `.claude/`, fichiers `TRANSFER*` et audits locaux ;
- `.venv/`, `node_modules/`, `.next/` et les caches : dépendances et compilation à reconstruire.

Pour retrouver aussi les données après une perte du disque, arrêter Radar et conserver une **copie privée** de `data/`, `.env` et des fichiers de connexion nécessaires dans un espace sécurisé séparé de GitHub. Les fichiers présents dans `data/backups/` restent sur le même PC tant qu'ils ne sont pas copiés ailleurs. La sauvegarde GitHub du code ne réalise pas cette copie privée.

## Récupérer le projet

Depuis un dossier destiné à accueillir une nouvelle copie, cloner la branche :

```powershell
git clone --branch sauvegarde-mvp-2026-10-11 https://github.com/Meryem-data-science/OPPORTUNITY-RADAR-AI.git OPPORTUNITY-RADAR-AI-TRANSFER
cd OPPORTUNITY-RADAR-AI-TRANSFER
```

On peut aussi sélectionner cette branche sur GitHub et utiliser **Code → Download ZIP** pour récupérer les fichiers. Un ZIP ne contient pas l'historique Git ; utiliser le clonage pour continuer les sauvegardes avec le bouton.

Installer **Python 3.12** et **Node.js**, puis reconstruire les dépendances depuis la racine de cette nouvelle copie :

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Push-Location .\apps\web
npm ci
npm run build
Pop-Location
```

Restaurer ensuite la copie privée de `data/`, `.env` et des autorisations. Les scripts attendent une base SQLite migrée au minimum jusqu'à `0026` et un profil déjà présent. `OPPORTUNITY_RADAR_PROFILE_ID` doit désigner ce profil. Les dépendances et la compilation se reconstruisent ; les offres, candidatures et décisions du profil ne se retrouvent pas à partir du code seul.

Enfin, double-cliquer sur **`Lancer-Radar.cmd`**. Le parcours de l'interface est décrit dans [mvp-interface.md](mvp-interface.md), et les détails du démarrage dans [startup.md](startup.md).
