# Propositions pour le dépôt DDU-SG5-SYMBOLISATION

Actuellement, le dépôt ne suffit pas tout à fait, il faut aussi :
- se passer d’étiquettes : le XML n’en donne ni le champ ni le placement
- deviner le symbole de 5 codes PSMV absents du style XML
- tenir une table des règles d’assiette SUP (`ref/sup_assiettes.csv` : quel code pour quel `typeass`)

Les propositions ci-dessous visent à ce que **le dépôt suffise, sans source complémentaire ni déduction**. 

## Pour que le dépôt suffise

**1. Un symbole explicite pour chaque code, dans une seule source de référence.** 

Les bibliothèques de styles QGIS (`PLU/QGIS/PLU_CNIG.xml`, `PSMV_CNIG.xml`, `SUP_CNIG.xml`) donnent un symbole nommé par code : c’est la seule source de la conversion, les QML ne sont pas lus. Elles couvrent tous les codes des grilles sauf 5 PSMV (version 5a838ff) :

| Code (grille) | XML | QML | Symbole utilisé |
|---------------|-----|-----|-----------------|
| `PSC-L_97-00_001`, `PSC-P_97-00_001` | **non** | oui | aucun (signalé) |
| `PSC-L_97-01_001`, `PSC-P_97-01_001` | **non** | **non** | aucun (signalé) |
| `PSC-S_26-01_001` | **non** | **non** | celui de `PSC-S_26-00_001`, par déduction (signalé) |

Les QML, eux, n’ont pas de catégorie pour 331 des 1 167 codes (QGIS leur applique son symbole par défaut). Pour les codes présents dans les deux, XML et QML donnent le même symbole. Le XML confirme 296 des 308 rattachements que la seule lecture des QML imposait de deviner, et en corrige 9 (PSMV `PSC-S_01-01`, `01-02`, `97-01` ; SUP `AR3` prohibition, `PT2` secondaire et spéciale…) ; il donne aussi un symbole aux 23 codes qui n’en avaient aucun (`SECTEUR_xx`, PSMV `PSC-x_05`, SUP `PM9`, `T5bis`).

Propositions :
- ajouter au XML les 5 codes manquants ;
- corriger `grille_sup.csv`, où `I3|gen_installannexegaz|ass_protection` figure deux fois alors que le XML a aussi `I3|gen_installannexegaz|ass_passage` (une des deux lignes est sans doute `ass_passage`) ;
- le dossier `SLD/` (SLD par code et SVG) n’est plus nécessaire à la réutilisation : le style XML embarque tous les pictos ;
- annoncer le XML comme référence, et générer les QML depuis lui (une catégorie par code), pour qu’ils ne divergent pas ;
- SUP : donner dans `grille_sup.csv` les attributs de chaque code (`typeass`, `type`), pour que les règles par attributs se déduisent du XML. `ASSIETTE_SUP_S.qml` dessine en effet autrement que le XML (A2 : 3 px tiretés « 3 6 » contre 1 mm tiretés « 6,3 6,3 »), et son filtre AR3 « Zone d’isolement » est tronqué à l’apostrophe.

**2. Des étiquettes complètes.** 

Le style XML n’a que des formats de texte (18 : `ZONE_U_001_txt`… Arial gras 14 pt, halo de 6 pt de la couleur de la zone ; 12 formats PSMV, dont les hauteurs `PSC-x_39-5x_001_txt`), mais leur section `<labelsettings>` est vide dans les trois fichiers : aucun réglage d’étiquette (champ ou expression affiché, placement, échelles d’affichage, nom du code), donc aucune étiquette n’est produite. Deux formats ne correspondent à aucun code (`ZONE_x_001_txt` PSMV, `planchecontact` PLU). Seuls les `.lyrx`, non lus, définissent des étiquettes complètes (zones PLU `_ge`), avec un halo de `AUc` noir contraire à la spécification et au catalogue.

Proposition : des réglages d’étiquette complets (champ, format, placement) dans le XML, conformes à la spécification et au catalogue (le halo de 6 pt des formats XML est deux fois plus épais que celui du catalogue).

Hauteurs PSMV (`PSC-x_39-50` à `52`) : les `.lyrx` affichent une valeur fixe (« 12 », « 20 », « 8 »), identique sur tous les objets ; le XML n’a qu’un format de texte (`PSC-L_39-50_001_txt`…) et des pictos de chiffres désactivés. Proposition : un réglage d’étiquette sur le champ `txt` de chaque prescription, dans le XML.

**3. Pas de couches désactivées.** 

Exemples : point blanc de `PSC-L_07-00_001`, pictos des PSMV `PSC-S_39-50` et `51`. À supprimer, ou à activer si elles font partie du symbole.

**4. Toutes les polices dans le dépôt.**

Les styles utilisent Wingdings, Marianne, Arial, Times New Roman et Tahoma (formats de texte), les `.lyrx` Century Gothic : aucune n’est dans le dépôt, qui fournit en revanche Wingdings 2 et 3, inutilisées par les QML. 

Proposition : déposer dans `__Ressources/polices/`, hors archive, chaque police citée, sous réserve de leur licence.

## Pour un dépôt plus simple à réutiliser

**5. Des QML homogènes.**

- Même découpage et même nommage pour PLU, PSMV et SUP ;
- champ `symbole` du standard (et non `SYMBOLE`) ;
- règles SUP filtrées sur `suptype` plutôt que par une expression sur `idass` ;
- SUP : un seul QML. Aujourd’hui, `sup_ass.qml` (par code) et `ASSIETTE_SUP_S.qml` (par attributs) décrivent les mêmes assiettes et doivent être tenus d’accord ; si `grille_sup.csv` donnait les attributs de chaque code (`suptype`, `typeass`…), comme les grilles PLU et PSMV, le second se déduirait du premier ;
- libellés de règle uniques ;
- pictogrammes SVG référencés par leur fichier plutôt qu’embarqués.

**6. Une image par code** à côté du catalogue PDF, générée depuis le XML, avec une table « code → fiche » : le contrôle n’aurait plus à découper le PDF.
