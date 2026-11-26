#!/usr/bin/env python3
"""
5. Rendu de référence QGIS : applique les QML CNIG aux couches de la grille de test (data/layer/compare.gpkg)
et écrit une image par couche, avec la même emprise et la même résolution que les rendus GeoServer.

Lancé par bin/compare.py avec l'interpréteur PyQGIS (QGIS_PYTHON) : render_qgis.py <jobs.json>
jobs.json : [{"gpkg", "layer", "qmls": [...], "bbox": [xmin, ymin, xmax, ymax], "size": [w, h], "out", "fonts"}]
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (  # noqa: E402
    QgsApplication, QgsCoordinateReferenceSystem, QgsMapRendererCustomPainterJob, QgsMapSettings,
    QgsRectangle, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QSize, Qt  # noqa: E402
from qgis.PyQt.QtGui import QColor, QFontDatabase, QImage, QPainter  # noqa: E402


def load_fonts(dirs):
    """Polices TrueType des symboles et des étiquettes, mises à disposition de QGIS."""
    for d in dirs:
        for f in Path(d).glob("*.[tT][tT][fF]"):
            QFontDatabase.addApplicationFont(str(f))


def styled_layer(gpkg, name, qml):
    """Couche `name` du GeoPackage, stylée par un QML CNIG."""
    layer = QgsVectorLayer(f"{gpkg}|layername={name}", name, "ogr")
    msg, ok = layer.loadNamedStyle(str(qml))
    if not ok:
        print(f"  ⚠ {qml}: {msg}")
    renderer = layer.renderer()
    # Les QML CNIG classent sur SYMBOLE ; la grille porte le champ « symbole »
    if renderer.type() == "categorizedSymbol":
        renderer.setClassAttribute("symbole")
    layer.setScaleBasedVisibility(False)
    return layer


def main():
    jobs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    app = QgsApplication([], False)
    app.initQgis()
    load_fonts(jobs[0].get("fonts", []) if jobs else [])
    for job in jobs:
        layers = [styled_layer(job["gpkg"], job["layer"], q) for q in job["qmls"]]
        settings = QgsMapSettings()
        settings.setLayers(layers[::-1])
        settings.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:2154"))
        settings.setExtent(QgsRectangle(*job["bbox"]))
        settings.setOutputSize(QSize(*job["size"]))
        settings.setOutputDpi(90.714)  # 0,28 mm/px, comme GeoServer
        settings.setBackgroundColor(QColor(Qt.white))
        image = QImage(QSize(*job["size"]), QImage.Format_ARGB32)
        image.fill(Qt.white)
        painter = QPainter(image)
        render = QgsMapRendererCustomPainterJob(settings, painter)
        render.start()
        render.waitForFinished()
        painter.end()
        image.save(job["out"], "png")
        print(f"  QGIS {job['layer']} → {Path(job['out']).name}")
    # exitQgis() + ramasse-miettes Python segfaultent sur les couches encore référencées : sortie directe
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
