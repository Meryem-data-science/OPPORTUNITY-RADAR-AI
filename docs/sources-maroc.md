# Sources Maroc ajoutées le 11 octobre 2026

## Accès vérifiés et contrat utilisé

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
