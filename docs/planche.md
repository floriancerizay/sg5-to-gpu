# Planche de contrôle

Comment `./run.sh` construit la planche publiée sur GitHub Pages (`report/`). Elle ne sert qu’au contrôle : les styles livrés n’en dépendent pas ([chaine.md](chaine.md)).

## Vignettes du catalogue : `bin/cnig_plates.py`

Rastérise les catalogues PDF, repère chaque fiche par son texte et découpe la vignette « rendu attendu ». Sorties : `report/cnig/*.png` et `report/cnig/plates.json` (code → vignette).

## Rendus et planche : `bin/compare.py`

1. Grille de test `data/layer/compare.gpkg`, avec les géométries des vignettes du catalogue.
2. Publication sur le GeoServer local (workspace `gpu_cmp`, recréé à chaque passe) des styles de production et des SLD par code.
3. Rendus à ≈ 1:1 800 : GeoServer (GetMap), et QGIS pour les styles CNIG d’origine (`render_qgis.py` : symboles du style XML, QML non lus).

Les codes sans symbole propre dans le style XML portent une pastille à côté du code : `déduit` (symbole du code principal) ou `aucun` (pas de style) ; les variantes « transparent » (`_000`) sont listées dans la fiche de leur type et sous-type, avec la pastille `transp.`. Le filtre regroupe conversion incomplète et écart au rendu QGIS sous « problème de rendu ».
4. Écart au rendu QGIS par code (`data/symbols/scores.json`), signalé au-delà de 6, seulement si c’est la seule erreur « ⚠ » du code (sinon, il découle le plus souvent de l’erreur déjà signalée). Le contour du polygone de test est comparé tel quel, l’intérieur après recalage de la trame : QGIS cale ses motifs sur l’objet, GeoServer sur l’image, et ce déphasage, invisible sur une carte, ne compte pas comme un écart.
5. Planche `report/compare.html` : une ligne par fiche, filtre par type de problème (types et motifs reconnus : `PROBLEM_TYPES` dans `compare.py`), impression A4 paysage depuis le navigateur ; le filtre s’applique aussi à l’impression et se partage par l’adresse (`compare.html?type=absent`).

## Diagnostics

Écrits par `convert_symbols.py` dans `data/symbols/issues.json`, affichés dans la colonne « problèmes du symbole d’origine » : « ⚠ » défaut ou partie non reproductible, « ℹ » information. Un même problème relevé sur plusieurs géométries d’une fiche n’est affiché qu’une fois, préfixé des géométries (« surf., pct. ») et avec les codes réunis (`PSC-S/P_40-50_001_txt`). L’icône de fichier à côté du message affiche, au survol, les fichiers attendus ou en cause (chemins du dépôt CNIG, sauf `data/…` produits par sg5-to-gpu) : style XML où le symbole du code est attendu, `.lyrx`, catalogue PDF et fiche attendue, `__Ressources/` pour une police… Format de `issues.json` : `{"domaine:code": [{"text": message, "files": [[chemin, rôle], …]}]}`.

| Thème | Messages |
|-------|----------|
| Code absent du style XML | rattaché au code principal (⚠) ; aucun style (⚠) |
| Conversion | couche, picto ou marque QGIS non traduits ; motif presque jointif (picto qui déborde du pas, aussi sous QGIS) ; pictogramme manquant |
| Étiquettes | réglage d’étiquette (champ, placement, échelles) absent de `<labelsettings>` du XML, seul un format de texte existe (ℹ) ; aucune étiquette produite |
| Polices | marque de police hors trame, rendu dépendant des polices de GeoServer (ℹ) |
| Rendu | écart au rendu QGIS > 6 (⚠, ajouté par `compare.py`, seulement sans autre « ⚠ » pour le code) ; fiche sans vignette dans le catalogue (ℹ) |
