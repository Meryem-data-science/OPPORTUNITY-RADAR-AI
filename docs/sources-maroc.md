# Sources du radar au Maroc

## Stage.ma, Stagiaires.ma et alertes emploi

- **Stage.ma** : collecteur public déjà activé pour la spécialité [Informatique](https://www.stage.ma/specialites/computer-science), avec 25 fiches maximum par cycle. La couverture reste limitée à cette rubrique ; les annonces expirées ou sans employeur utilisable sont écartées.
- **Stagiaires.ma** : collecteur public déjà activé, découverte par le sitemap officiel et 25 fiches maximum par cycle.
- **LinkedIn** et **Indeed** : annonces extraites des alertes reçues dans le compte Gmail autorisé, au maximum 50 emails des sept derniers jours par source. La même connexion en lecture seule couvre les deux plateformes. Lancer **Connecter-Alertes-Emploi.cmd** dans le dossier du projet si l'autorisation est absente ou expirée ; l'ancien lanceur LinkedIn reste compatible. Voir le [guide de connexion et de création des alertes](connexion-linkedin.md).

Ces collectes couvrent les pages et emails effectivement lus. Elles ne garantissent ni la totalité des catalogues ni un nombre fixe d'offres. Une annonce sans localisation marocaine confirmée n'apparaît pas dans la sélection Maroc. La date d'un email ne prouve pas l'ouverture du poste et ne réactive pas une offre fermée. Un format d'alerte Indeed non reconnu fait apparaître une erreur de source.

La page **Sources** montre la dernière collecte enregistrée pour chacune des sources activées et conserve les diagnostics d'échec. L'ajout du collecteur Indeed ne constitue pas à lui seul une validation de la connexion Gmail ou des emails présents.

Le parseur Indeed accepte les liens directs portant une clé d'annonce explicite sur `ma.indeed.com`, `www.indeed.com` ou `indeed.com`. Il peut aussi décoder localement une destination lisible dans un lien `cts.indeed.com`, puis lui appliquer les mêmes contrôles. Il retire les paramètres de suivi et conserve une URL unique par clé pour éviter les doublons entre collectes. Les redirections opaques, notamment `engage.indeed.com`, ne sont pas ouvertes. Chaque email est traité en bloc : une carte non reconnue ou ambiguë fait écarter cet email entier. Les autres emails entièrement reconnus peuvent être importés ; la source est alors signalée en échec avec un diagnostic de collecte partielle et les compteurs réels des offres conservées. Si aucun email ne peut être extrait, la collecte échoue sans nouvelle offre. Les autres sources continuent. Les annonces Indeed restent **à vérifier**, sans badge d'ouverture déduit de l'email.

Le filtre Indeed initial ne ciblait que `alert@indeed.com`. Il couvre aussi `donotreply@jobalert.indeed.com` et `donotreply@match.indeed.com`, avec la même limite de sept jours et de 50 emails. Une recherche vide ne prouve pas que les alertes sont désactivées : vérifier d'abord le compte Gmail autorisé, la date des emails et leur expéditeur. Si les alertes sont déjà reçues sur ce compte, aucune nouvelle activation Indeed n'est nécessaire.

## Sources publiques ajoutées le 11 octobre 2026

### Accès vérifiés et contrat utilisé

| Source | Entrée publique | Accès et sélection |
|---|---|---|
| SQLI | https://www.sqli.com/robots.txt → sitemap déclaré https://www.sqli.com/sitemap.xml | HTTP 200 ; annonces `/fr-fr/carriere/nos-offres-demploi/<slug>` ; pays confirmé par `data-country`, ville par `data-agency`, titre par H1 et `data-job-title`, contrat par `data-job-contract`. |
| Welo Data | https://api.lever.co/v0/postings/weloglobal?mode=json | Catalogue public sans authentification ; `categories.location` / `allLocations` ou pays principal MA. |
| Yassir | https://api.lever.co/v0/postings/Yassir?mode=json | Même contrat public Lever ; original `hostedUrl`, candidature `applyUrl`, descriptions et sections fournies. |

Documentation primaire Lever : https://github.com/lever/postings-api. Le `robots.txt` de https://api.lever.co/robots.txt autorise `/` et indique un délai d’une seconde, respecté par le collecteur. Le collecteur n’accède qu’au serveur global configuré ; aucun endpoint interne ou régional n’est deviné.

Exemples de pages SQLI vérifiées :

- https://www.sqli.com/fr-fr/carriere/nos-offres-demploi/architecte-data-databricks-5986 — Casablanca, Maroc, CDI.
- https://www.sqli.com/fr-fr/carriere/nos-offres-demploi/ingenieur-data-gcp-google-cloud-platform-fh-5993 — Rabat, Maroc, CDI.

Le catalogue public Welo contenait une mission « Generative AI Analyst | Arabic (Morocco) ». Ces observations concernent la date de vérification, pas une promesse de disponibilité future.

## Bornes et disponibilité

SQLI : dix sitemaps et cinquante descriptions par cycle, huit Mo par document ; les mots du slug servent uniquement à découvrir des candidats. Une description étrangère est écartée, la qualification Data/IA définit ensuite la pertinence. `lastmod` ordonne les lectures sans devenir une date de publication. Au-delà de cinquante candidates, quarante pages récentes sont lues et dix pages anciennes tournent selon l’heure UTC, pour élargir progressivement la couverture sans dépasser la borne.

Lever : cent annonces par page, vingt pages maximum, huit Mo par réponse. Une réponse invalide, un dépassement, des identifiants répétés ou un accès refusé font échouer la source ; aucune liste partielle n'est présentée comme une collecte réussie. La mention « remote » seule ne confirme pas le Maroc.

Le contrôle de disponibilité partage la borne de cinquante annonces hors Greenhouse par cycle et le délai de revérification de six heures. Le retrait exact d’une annonce Lever est vérifié sur son endpoint public UUID ; l’absence d’une annonce dans une sélection géographique ne sert jamais de preuve de fermeture. SQLI reste non vérifiable lorsque son formulaire externe est inaccessible et que sa page ne fournit pas de preuve d’expiration. Les erreurs réseau, restrictions et demandes de connexion ne ferment ni ne réactivent une annonce.

## Sources examinées mais non activées

- SmartRecruiters : https://api.smartrecruiters.com/robots.txt interdit le robot général (`User-agent: *`, `Disallow: /`). L’exception LinkedInBot n’est pas utilisée.
- Emploi.ma, Devoteam et ALTEN : lecture automatique des règles refusée lors de cette séance.
- ReKrute : sitemap accessible, descriptions refusées (403).

Ces plateformes ne sont pas exposées comme de nouvelles sources opérationnelles. Les annonces officielles SQLI sont lues sur SQLI, sans requête à l’API SmartRecruiters interdite.
