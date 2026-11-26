# Planche de contrôle

Comment `./run.sh` construit la planche publiée sur GitHub Pages (`report/`). Elle ne sert qu’au contrôle : les styles livrés n’en dépendent pas ([chaine.md](chaine.md)).

## Vignettes du catalogue : `bin/cnig_plates.py`

Rastérise les catalogues PDF, repère chaque fiche par son texte et découpe la vignette « rendu attendu ». Sorties : `report/cnig/*.png` et `report/cnig/plates.json` (code → vignette).

## Rendus et planche : `bin/compare.py`

1. Grille de test `data/layer/compare.gpkg`, avec les géométries des vignettes du catalogue.
2. Publication sur le GeoServer local (workspace `gpu_cmp`, recréé à chaque passe) des styles de production et des SLD par code.
3. Rendus à ≈ 1:1 800 : GeoServer (GetMap), et QGIS pour les QML d’origine (`render_qgis.py`).
4. Écart au rendu QGIS par code (`data/symbols/scores.json`), signalé au-delà de 6. Le contour du polygone de test est comparé tel quel, l’intérieur après recalage de la trame : QGIS cale ses motifs sur l’objet, GeoServer sur l’image, et ce déphasage, invisible sur une carte, ne compte pas comme un écart.
5. Planche `report/compare.html` : une ligne par fiche, filtre par type de problème (types et motifs reconnus : `PROBLEM_TYPES` dans `compare.py`), impression A4 paysage depuis le navigateur ; le filtre s’applique aussi à l’impression et se partage par l’adresse (`compare.html?type=absent`).

## Diagnostics

Écrits par `convert_symbols.py` dans `data/symbols/issues.json`, affichés dans la colonne « problèmes du symbole d’origine » : « ⚠ » défaut ou partie non reproductible, « ℹ » information. Sous chaque problème, une recommandation (`RECOMMENDATIONS` dans `compare.py`) indique quoi corriger, le plus souvent dans le dépôt CNIG.

| Thème | Messages |
|-------|----------|
| Code sans symbole dans les QML | rattaché à un code au SLD identique (ℹ) ou au code principal (⚠, avec mention si son SLD par code diffère) ; aucun style (⚠) |
| Conversion | couche, picto ou marque QGIS non traduits ; motif presque jointif (picto qui déborde du pas, aussi sous QGIS) ; pictogramme manquant |
| Étiquettes | reprise du `.lyrx` (ℹ) ; texte constant non repris (ℹ) ; police remplacée (⚠) ; halo différent de la couleur de la zone (⚠) |
| Polices | marque de police hors trame, rendu dépendant des polices de GeoServer (ℹ) |
| Rendu | écart au rendu QGIS > 6 (⚠, ajouté par `compare.py`) ; fiche sans vignette dans le catalogue (ℹ) |
