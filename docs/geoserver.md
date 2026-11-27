# Comportements GeoServer et limites

Constats qui ont guidé la conversion (vue d’ensemble : [README](../README.md), détail des étapes : [chaine.md](chaine.md)).

## Comportements GeoServer constatés

Mesurés sur le GeoServer local ; ils valent aussi pour les styles de production.

| Comportement | Conséquence | Traitement |
|--------------|-------------|------------|
| `Rotation` ignorée pour une `ExternalGraphic` de `GraphicFill`, appliquée **dans** chaque tuile pour une marque | flèches mal orientées ; hachure `horline` à 30° en tirets | rotation intégrée à la tuile SVG |
| Image SVG de `GraphicFill` rastérisée à ⌈taille⌉ et entourée d’une **bordure transparente de 1 px** | trames de pictos trop lâches, trames superposées qui dérivent | tuile au pas exact, `graphic-margin` -1 |
| Tuile arrondie au pixel | trame fine rendue en aplat ; coutures | tuile répétée k × k sur des pixels entiers |
| Caractère de police d’une marque étiré à la taille demandée | motifs de police énormes (AC3, PM4) | caractères convertis en tracés SVG à la taille QGIS |
| `DisplacementY` > 0 **descend** le picto (comme QGIS, contrairement à la spécification SE) | — | décalages QGIS repris tels quels |
| `PerpendicularOffset` > 0 à **gauche** du sens de la ligne ; sur un contour de polygone, toujours **vers l’extérieur** (anneaux réorientés) ; QGIS décale un contour vers l’intérieur | liserés et pictos du mauvais côté | signe adapté (ligne / contour) |
| `Rotation` ignorée et `Displacement` en coordonnées écran dans un `GraphicStroke` | tirets couchés le long de la ligne, pictos poussés à côté | rotation intégrée au SVG, décalage en `stroke-dashoffset` / `PerpendicularOffset` |
| `GraphicStroke` d’un contour de polygone : picto orienté à l’inverse de QGIS | triangles pointant vers l’extérieur | image retournée de 180° |
| `GraphicStroke` : picto centré sur l’extrémité de la ligne non dessiné | 1er picto manquant | `PointSymbolizer` sur `startPoint` |
| `stroke-dashoffset` négatif refusé ; au-delà de l’intervalle du tireté d’un `GraphicStroke` : **plantage de la couche** | couche non rendue | décalage ramené dans [0, intervalle), décalage commun des tiretés du symbole |
| `stroke-dasharray` à intervalle nul | plantage de la couche | tireté d’origine conservé |
| Images externes **en cache par URL** | un picto remplacé sous le même nom reste affiché dans son ancienne version | empreinte du contenu dans l’URL ; à prévoir au déploiement |
| Images `http(s)` d’une `ExternalGraphic` **non chargées, sans message** tant que le contrôle d’URL (*URL Checks*, 2.28) n’a pas de règle qui les autorise | pictos et trames absents, aucune requête émise, aucune erreur au journal | au déploiement : règle autorisant `SLD_IMG_URL` |
| Images `http(s)` chargées **une à une** pendant le rendu, à la 1re utilisation de chaque URL (puis en cache) | après redémarrage, 1er GetMap d’une couche = nombre d’images × latence (106 images à 250 ms : 27 s pour `prescription_surf`) ; ensuite, aucun surcoût mesuré | préchauffer le cache après déploiement / redémarrage, ou servir les images depuis le data_dir |
| Trame native au pas fractionnaire : tuile arrondie au pixel, trait calé sur la grille | densité faussée (trames de points fins), traits plus nets et plus foncés que QGIS | trame native seulement au pas entier, sinon tuile SVG |
| Marque `wkt://` : origine de la géométrie sur le point (pas de recentrage), y vers le haut, **hauteur de l’emprise mise à `Size`** ; emprise d’un `POLYGON` = son anneau extérieur | picto en plusieurs morceaux écrit en `POLYGON` : marque ~3 fois trop grande (INF-P_16) | coordonnées centrées sur le viewBox, `MULTIPOLYGON` (anneaux classés par imbrication), `Size` × hauteur du tracé / hauteur du viewBox |
| Marque dans un `GraphicStroke` : espacée et orientée autrement qu’une image | vagues jointives (INF-L_19), flèches tournées (PSC-L_39) | pictos de ligne laissés en SVG |

## Limites

- Les trames sont calées sur l’origine de l’image par GeoServer, sur l’objet par QGIS : même dessin, phase différente (l’écart mesuré en tient compte en partie).
- `MarkerLine` à placement « au milieu de la ligne » non traduite (signalée si active). Seules occurrences CNIG : PSMV PSC-S_39-50 / 51, désactivées dans le QML. GeoServer saurait la rendre par un `TextSymbolizer` à `Graphic` (procédé des styles de production pour 39-50 / 51).
- Picto du début de ligne ajouté seulement pour les pictos sans décalage latéral (1er tiret de PSC-L_25 absent).
- Orientation des pictos sur un contour : réglée et vérifiée sur des anneaux extérieurs en sens horaire (convention ArcGIS des données de test) ; non vérifiée sur des anneaux saisis dans l’autre sens.
- Marques de police hors trames et hachures tiretées à 45° peu denses restent rendues par GeoServer.
- QGIS ne rend pas le tireté d’une hachure très dense (`sup:EL7|gen|ass`, tirets de 3 pt tous les 3 pt) : ses tirets se rejoignent en traits presque continus, alors que le catalogue montre bien un tireté. L’écart au rendu QGIS de ce code vient de QGIS, pas de la conversion.
- QGIS n’affiche pas les étiquettes des zones `_ge` : leur écart mesuré est surestimé.
- SLD par couche : sans style CNIG, `secteur_cc` et les PSMV `PSC-L_05` / `PSC-P_05` gardent les règles de production ; pas de SLD par symbole pour `secteur_cc`.
