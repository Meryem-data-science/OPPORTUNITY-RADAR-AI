# Instructions pour les agents — Opportunity Radar AI

## Projet

Application locale pour les opportunités Data & IA au Maroc, avec FastAPI, SQLite et Next.js. Le parcours MVP est décrit dans `docs/mvp-interface.md`. Les scripts Windows sont dans `scripts/` ; `Lancer-Radar.cmd` ouvre le site. Ne pas ajouter ou exécuter de tests sans demande de l'utilisateur.

## Sauvegarde en fin de séance

Le 11 octobre 2026, l'utilisateur a autorisé la sauvegarde du code sur son dépôt GitHub **après chaque séance de développement**, sans redemander cette confirmation.

- Dépôt : `Meryem-data-science/OPPORTUNITY-RADAR-AI`.
- Branche de sauvegarde : `sauvegarde-mvp-2026-10-11`.
- Avant de terminer une séance qui modifie ce projet, relire le statut Git et le diff, puis enregistrer les changements de code autorisés dans un commit descriptif et les envoyer sur cette branche.
- Respecter `.gitignore`. Inclure les fichiers source, assets, licences, scripts et documentation nécessaires à la restauration. Exclure `data/`, `.env` et les autres paramètres privés, `.secrets/`, les jetons, CV, profils personnels, `.claude/`, `TRANSFER*`, les audits locaux, `.venv/`, `node_modules/`, `.next/` et les caches.
- Préserver les changements existants hors du périmètre de la séance ; ne pas les écraser ou les supprimer. Inspecter les fichiers ajoutés avant de les publier, puisque le dépôt est public.
- Préserver l'historique : aucun push forcé, reset destructif, suppression de branche ou merge automatique. Si la branche distante diverge, garder le commit local et signaler la situation.
- Si aucun fichier de code n'a changé, ne pas créer de commit vide. Vérifier également si un commit local autorisé reste à envoyer.
- Utiliser Git en local si l'authentification fonctionne. Si elle est indisponible, le connecteur GitHub autorisé peut publier les mêmes changements sur la branche de sauvegarde, en préservant l'historique distant et en documentant l'état local.
- Confirmer la sauvegarde seulement après vérification du commit sur la branche distante. Dans le compte rendu final, indiquer la branche et le commit, ou la raison précise d'un échec.

Ces instructions concernent les séances de travail avec l'agent. Pour les modifications faites ailleurs, le lanceur `Sauvegarder-GitHub.cmd` permet une sauvegarde volontaire. Aucun minuteur, tâche planifiée ou événement de fermeture n'est installé par ces instructions.
