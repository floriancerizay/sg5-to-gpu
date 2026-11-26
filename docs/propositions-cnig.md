# Propositions pour le dépôt DDU-SG5-SYMBOLISATION

Actuellement, les QML ne suffisent pas, il faut aussi :
- lire les `.lyrx` pour les étiquettes
- comparer les SLD par code et deviner le symbole des sous-codes

Les propositions ci-dessous visent à ce que **le dépôt suffise, sans source complémentaire ni déduction**. 

## Pour que les QML suffisent

**1. Un symbole explicite pour chaque code.** 

Exemple, prescriptions surfaciques PSMV, type 01 :

| Code (grille) | Libellé (grille) | Catégorie dans le QML | SLD par code |
|---------------|------------------|-----------------------|--------------|
| `PSC-S_01-00_001` | Espace boisé classé | oui | oui |
| `PSC-S_01-01_001` | Espace boisé classé à protéger ou conserver | **non** | oui |
| `PSC-S_01-02_001` | Espace boisé classé à créer | **non** | oui |
| `PSC-S_01-03_001` | Espace boisé classé significatif au titre de la loi littoral | oui | oui |

Sources : `__outils-agp-python-gdb__/grille_psmv_psc.csv`, `PSMV/QGIS/QML/psmv_psc_s.qml` (ex. `<category value="PSC-S_01-00_001" symbol="0"/>`), `PSMV/SLD/PSC-S_01-0x_001.sld`. Les quatre codes ont un SLD par code, mais le QML n’en style que deux.

331 des 1 167 codes n’ont pas de catégorie dans les QML (QGIS leur applique son symbole par défaut). 

Le script en rattache 308 à un autre code par déduction ; pour 158 d’entre eux, le SLD par code du sous-code contredit ce choix (ex. PSMV `PSC-S_01-01_001`). 
23 restent sans style (`SECTEUR_xx`, PSMV `PSC-x_05`, SUP `PM9`, `T5bis`). 

Deux solutions possibles :
- une catégorie QML par code, quitte à répéter le symbole du code principal ;
- une colonne « symbole de référence » dans les grilles, qui désigne pour chaque sous-code le code dont il reprend le symbole.

**2. Les étiquettes dans les QML.** 

Seuls les `.lyrx` définissent des étiquettes (zones PLU `_ge`), avec un halo de `AUc` noir contraire à la spécification et au catalogue. `plu_zone_urba_ge.qml` active les étiquettes mais n’en définit aucune. 

Proposition : des règles d’étiquetage dans les QML de zonage, conformes à la spécification.

*Question au CNIG* : les prescriptions de hauteur PSMV (`PSC-x_39-50` à `52`) ont dans les `.lyrx` une étiquette à valeur fixe (« 12 », « 20 », « 8 »), identique sur tous les objets, absente des QML et du catalogue. Faut-il afficher la hauteur de chaque prescription (quel attribut ?), une valeur d’exemple, ou rien ?

**3. Pas de couches désactivées.** 

Exemples : point blanc de `PSC-L_07-00_001`, pictos des PSMV `PSC-S_39-50` et `51`. À supprimer, ou à activer si elles font partie du symbole.

**4. Toutes les polices dans le dépôt.**

Les QML utilisent Wingdings et Marianne (`ASSIETTE_SUP_S.qml`), les `.lyrx` Arial et Century Gothic : aucune n’est dans le dépôt, qui fournit en revanche Wingdings 2 et 3, inutilisées par les QML. 

Proposition : déposer dans `__Ressources/polices/`, hors archive, chaque police citée, sous réserve de leur licence.

## Pour un dépôt plus simple à réutiliser

**5. Des QML homogènes.**

- Même découpage et même nommage pour PLU, PSMV et SUP ;
- champ `symbole` du standard (et non `SYMBOLE`) ;
- règles SUP filtrées sur `suptype` plutôt que par une expression sur `idass` ;
- libellés de règle uniques ;
- pictogrammes SVG référencés par leur fichier plutôt qu’embarqués.

**6. Une image par code** à côté du catalogue PDF, générée depuis les QML, avec une table « code → fiche » : le contrôle n’aurait plus à découper le PDF.
