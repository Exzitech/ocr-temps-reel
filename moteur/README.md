# Coût des plats

Outil d'[ExziHub](https://github.com/Exzitech/exzihub-app) : calcule le coût réel d'un plat en
lisant par OCR les messages d'achat affichés par le logiciel de commande du grossiste
(le « chat »), sans saisie manuelle pendant les commandes.

Ce dossier contient le **moteur** (Python). Son écran est un plugin du hub
([ExziHub](https://github.com/Exzitech/exzihub-app), non publié ici). Le hub le lance avec `pythonw serveur.py`
(bouton ▶) et lui parle en WebSocket sur `ws://localhost:8000/ws` (port réglable
dans `config.json`, clé `serveur`).

## Installation manuelle

`installer.bat` s'occupe de tout. Sans lui, depuis ce dossier :

```
pip install -r requirements.txt
```

Il faut Python 3.9+, Tesseract (`winget install UB-Mannheim.TesseractOCR`) et
le pack de langue
[`fra.traineddata`](https://github.com/tesseract-ocr/tessdata/raw/main/fra.traineddata),
à placer dans le `tessdata` de Tesseract ou dans `tessdata/` ici (renseigner
alors `ocr.tessdata_dir` dans `config.json` ; un chemin relatif suffit).

`config.json` est créé au premier lancement à partir de `config.exemple.json`.
Il n'est pas versionné : zone de capture et chemins propres à la machine. Si
Tesseract ou la langue manquent, le moteur le dit au démarrage.

## Premier lancement

1. dans le logiciel du grossiste, afficher le fil des messages de commande seul,
   et l'agrandir au maximum (voir « Rafales » plus bas) ;
2. dans ExziHub, ouvrir **Coût des plats** et cliquer **▶ Lancer le backend** ;
3. **Zone de capture** : tracer un rectangle sur ce chat ;
4. **Catalogue** (ou **Nouvelle session** pour saisir des noms) : choisir les
   objets à cuisiner, 20 recettes au maximum ;
5. acheter chez le grossiste normalement : chaque ligne d'achat est lue et comptée.

## Écran

- **Liste d'achats** : tous les ingrédients de toutes les recettes,
  additionnés en tenant compte du nombre de plats. Colonnes En stock / Besoin
  / Acheté / Moyenne / Prix u. / Total. **En stock** (− / +, molette) = ce que
  tu as déjà en chambre froide ou en réserve : compte pour le besoin, pas pour la
  moyenne. **Prix u.** = prix unitaire imposé à la main, figé pour toutes les
  recettes qui utilisent l'ingrédient (utile quand tout est déjà en stock) ;
  vide = prix moyen d'achat. Clic sur un nom = copié pour la recherche chez le grossiste,
  sans notification.
  - L'ordre ne bouge pas tout seul : les nouveaux ingrédients s'ajoutent en
    bas, **⇅ Trier** remet ce qui reste à acheter en haut.
- **⧉ Liste flottante** : ingrédient, stock, besoin, acheté, prix unitaire,
  dans une fenêtre sombre au-dessus du logiciel du fournisseur, hauteur ajustée au nombre de
  lignes, opacité réglable. Les achats **à confirmer** y apparaissent en
  pop-up avec `OK` / `✕`, suivis d'un message de retour ; les erreurs du
  moteur s'affichent en rouge. `💰` ouvre le récap flottant.
- **⧉ Récap flottant** : nom du plat (clic = copié) et prix de vente total,
  modifiable. `🛒` rouvre la liste flottante. Chaque fenêtre flottante garde
  sa position et sa taille.
- **Recettes** : par recette, progression par ingrédient (🔒 prix figé, ⏳ en
  cours), coût total dès que tout est figé, **× N** plats (toutes les
  quantités et les seuils suivent), **✕** pour retirer la recette.
  - `+1` / `+n` : achat raté par l'OCR. Sans euros saisis, le prix moyen
    courant est appliqué, donc la moyenne n'est pas déformée.
  - `✎` : corriger le cumul exact (les prix figés automatiquement sont
    recalculés), imposer ou défiger un prix, annuler une saisie manuelle.
- **Catalogue** : les plats du catalogue de recettes, par famille, avec image
  et liste d'ingrédients. **Ajouter** place le plat dans la session **sans
  remettre les compteurs à zéro** (ou démarre la session).
- **Récap / Bénéfices** : prix d'achat (total et unitaire) et de vente **du
  lot**. `bénéfice = vente − taxe % × vente − coût d'achat` : la commission de vente se
  prélève sur le prix de vente, pas sur la marge (`vente.taxe_pourcent`).
  - `📌` : garder le plat (quantité, prix de vente) pour une prochaine session.
  - `↺` (reset) : le lot est cuisiné. Ses ingrédients sont retirés des achats
    (au prix moyen, la moyenne du reste ne bouge pas) puis du stock ; le
    surplus reste aux autres recettes. Prix figés effacés, prix de vente
    conservé, et on choisit la quantité du prochain lot.
  - `✕` : retirer la recette (deux clics).
- **À faire plus tard** : plats gardés avec `📌`, enregistrés dans
  `config.json` (`session.plats_memorises`). **Ajouter** / **Tout ajouter**
  les remet dans la session avec leur quantité et leur prix de vente, sans
  remettre les compteurs à zéro.
- **À confirmer** : achats reconnus avec un score entre 70 et 90, ou relus
  après une perte d'alignement (badge **relu**, voir plus bas), comptés
  seulement après `OK`. **Non identifiés** : `Lier` rattache un nom lu à un
  ingrédient et rattrape ses achats.
- **Pause** : les achats faits pendant la pause ne sont pas comptés.
- Barre d'état : taille de la queue OCR (si elle grossit, l'OCR ne suit plus ;
  pleine, un bandeau propose **Vider la queue**), durée d'OCR, images jetées, resynchronisations, achats comptés. Onglet
  **Journal** ; journal complet dans `cout-plats.log`.

## Réglage de la zone de capture

Bouton **Zone de capture** du hub, ou `python selecteur_zone.py`. Un voile
couvre tous les écrans (l'ancienne zone en pointillés) : tracer un rectangle,
c'est enregistré dans `config.json` (`Échap` pour annuler) et la capture
redémarre sans perdre les compteurs. Les montages multi-écrans sont gérés, y
compris un écran **à gauche** du principal (coordonnées négatives).

À la main, tout est dans `config.json` :

```json
"capture": {
  "monitor": 1,
  "zone": { "left": 20, "top": 780, "width": 900, "height": 260 },
  "interval_s": 0.4,
  "queue_maxsize": 4
}
```

Pour vérifier ce que l'OCR lit réellement :

```
python outil_zone.py                 # zone de config.json
python outil_zone.py 20 780 900 260  # left top width height
```

`zone_brute.png` et `zone_traitee.png` (ce que voit Tesseract), puis texte brut,
messages découpés et achats reconnus.

### Rafales d'achats identiques — agrandis le chat

Si tu achètes 40 fois le même item **au même prix** à 3-8 achats/seconde, le
chat affiche 40 lignes rigoureusement identiques : deux captures successives
sont **identiques au pixel près**, que le chat ait défilé ou non.

| Lignes visibles dans le chat | Achats comptés sur 40 |
|---|---|
| 10 | 10 (25 %) |
| 20 | 20 (50 %) |
| 25 | 25 (62 %) |
| 50 | **40 (100 %)** |

**Règle : le chat doit afficher au moins autant de lignes que ta plus grosse
rafale.** Quand il est saturé par un achat répété, le hub l'affiche en alerte :
le total de cet ingrédient est à vérifier au bouton `+n`.

### Les fenêtres de l'outil sont invisibles pour sa propre capture

Bug réel : l'ancienne version tournait dans une console qui affichait le
journal, avec des fenêtres *toujours au-dessus*. Posées sur le chat, elles
étaient **relues par l'OCR** : `[22:36] 10 x [Filet de sanglier] (3 993 euros)`
redevenait un achat, dont le comptage produisait une nouvelle ligne relue à son
tour. Un seul achat compté **9 fois**. D'où :

- toutes les fenêtres sont exclues de la capture (`SetWindowDisplayAffinity`,
  Windows 10 2004+ et 11 ; `contentProtected` côté hub) ;
- le moteur tourne **sans console** (`pythonw`).

## Logique de prix

- Par ressource R : `qty_cumulée[R] += quantité`, `euros_cumulés[R] += prix`.
- `prix_moyen[R] = euros_cumulés[R] / qty_cumulée[R]`, à tout instant.
- `stock[R]` (déjà possédé) compte pour atteindre le requis, pas pour la moyenne.
- Pour chaque recette et chaque ingrédient R : dès que
  `qty_cumulée[R] + stock[R] >= quantité requise par CETTE recette` (et au moins
  une unité achetée), on fige `prix_figé[recette][R] = prix_moyen[R]`, une fois.
- Le figeage est **indépendant par recette** : deux recettes utilisant la même
  ressource avec des quantités différentes se figent à des moments, donc à des
  prix, potentiellement différents. C'est voulu.
- Coût d'une recette = Σ `quantité_requise × prix_figé`.

## Points d'implémentation

- **Découpage OCR** : jamais sur les retours à la ligne (un nom long est coupé
  sur deux lignes), mais sur `\[\d{2}:\d{2}\]`.
- **Extraction** : horodatage, quantité, nom entre crochets, prix. Ventes,
  banque et messages système ne matchent pas. **Le logiciel du fournisseur sépare les milliers par
  une espace** `(1 067 euros)` : espace, insécable, fine, point ou virgule sont
  acceptés, sinon tout achat à 1000 euros ou plus disparaissait.
- **Déduplication par position, jamais par contenu** : on cherche le plus long
  recouvrement entre la fin du flux accepté et le début de la nouvelle capture ;
  trois achats identiques d'affilée comptent pour trois (`parsing.DedupeurDeFlux`).
  - l'alignement **tolère des écarts** à l'intérieur du recouvrement ;
  - s'il échoue, on se **resynchronise sans recompter** la fenêtre, sauf les
    messages dont la minute est **strictement postérieure** à tout ce qui a déjà
    été lu (le premier achat d'une rafale était perdu). Même minute ou passage
    de minuit : ignorés, par prudence ;
  - l'historique garde **toujours au moins une capture entière**. Bug réel :
    un chat agrandi affichait 62 messages pour 60 mémorisés, et toutes les
    captures passaient en resynchronisation (935 dans une soirée) ;
  - avant de se resynchroniser, on cherche le **dernier message compté** : s'il
    apparaît une seule fois dans la capture, ce qui le suit est nouveau (une
    ligne mal lue dans une petite fenêtre suffisait à perdre l'achat suivant) ;
  - les achats de la dernière minute écartés par une resynchronisation sont
    **proposés à confirmer** s'ils sont affichés plus de fois qu'ils n'ont été
    comptés, proposés ou refusés ; jamais comptés d'office.
- **Catalogue de recettes** : les noms sont **désaccentués** avant la recherche.
  Toute réponse est plafonnée à 50 éléments : recettes et noms sont chargés par
  lots. Les recettes sont chargées **par identifiant de plat** (aucune ambiguïté
  de nom). L'adresse de l'API (`catalogue.py`) est un exemple, à configurer.
- **Matching** : `rapidfuzz` (`token_sort_ratio`) contre l'union des
  ingrédients actifs, **sans tenir compte des accents** (`Wagyū`, dont le `ū`
  n'est pas lisible par l'OCR, reconnu depuis `Wagyu`). ≥ 90 accepté, 70–90 à confirmer, < 70 non identifié.
- **Pipeline** : thread `mss` → `queue.Queue` → thread `pytesseract`
  (`--psm 6` + whitelist). Un OCR coûte ~300 ms : `interval_s` à 0.4 s, file
  courte qui jette l'image la plus ancienne, images identiques sautées.
- **Whitelist et chemins** : la whitelist (qui contient une espace) passe par
  `whitelist.tessconfig`, les chemins sont normalisés (8.3 s'ils contiennent
  une espace) à cause du découpage `shlex` de `pytesseract` sous Windows.
- **config.json** : le serveur n'y réécrit que les clés modifiées (zone, items),
  pour ne jamais y figer des valeurs résolues (tessdata absolu).

## Fichiers

| Fichier | Rôle |
|---|---|
| `serveur.py` | point d'entrée : moteur exposé au hub en WebSocket |
| `journal.py` | configuration du journal `cout-plats.log` |
| `config.py` / `config.json` | configuration (zone, seuils, OCR, port) ; modèle `config.exemple.json` |
| `catalogue.py` | client catalogue de recettes (recettes par nom ou par id) |
| `ocr.py` | pipeline capture → OCR, exclusion de capture |
| `parsing.py` | découpage, regex d'achat, déduplication positionnelle |
| `tracker.py` | session : cumuls, stock, matching, figeage, corrections |
| `moteur.py` | colle OCR → parsing → session |
| `selecteur_zone.py` | sélection de la zone à la souris (multi-écrans) |
| `outil_zone.py` | ce que l'OCR lit dans la zone |
| `demo_chat.py` | faux fil de commandes, pour essayer sans logiciel fournisseur |
| `tests/` | tests du moteur |

## Essayer sans logiciel fournisseur

`python demo_chat.py` ouvre un **faux chat** qui débite de vrais messages
d'achat (la zone par défaut est calée dessus) ; lancer le moteur depuis le hub,
puis ajouter la Salade de fruits. `Rafale (3/s)` simule une rafale, `Vente (ignorée)` un
message qui ne doit pas être compté.

## Tests

Depuis ce dossier :

```
python -m unittest discover -s tests -t .
```

- `test_logique.py` — parsing, dédup, figeage par recette, stock, ajout/retrait
  de recettes, récap, matching ;
- `test_serveur.py` — vrai serveur WebSocket : état à la connexion, commandes,
  catalogue (sans jamais écrire le vrai `config.json`) ;
- `test_moteur.py`, `test_pipeline.py` — flux de captures, politique de file ;
- `test_zone.py` — écrans, glisser-déposer simulé, zone capturable ;
- `test_exclusion.py` — une fenêtre masquée n'est plus relue par l'OCR ;
  moteur lancé sans console ;
- `test_ocr_reel.py` — **vrai** Tesseract sur une image de chat fabriquée
  (ignoré si Tesseract manque).
