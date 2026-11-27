#!/usr/bin/env python3
"""
5. Rendu de référence QGIS : applique les symboles des styles XML du CNIG (PLU_CNIG.xml…) aux couches de la grille
de test (data/layer/compare.gpkg), en catégories sur le champ « symbole » (les QML ne sont pas lus), et écrit une
image par couche, avec la même emprise et la même résolution que les rendus GeoServer.

Lancé par bin/compare.py avec l'interpréteur PyQGIS (QGIS_PYTHON) : render_qgis.py <jobs.json>
jobs.json : [{"gpkg", "layer", "style": xml, "bbox": [xmin, ymin, xmax, ymax], "size": [w, h], "out", "fonts"}]
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsMapRendererCustomPainterJob,
    QgsMapSettings,
    QgsRectangle,
    QgsRendererCategory,
    QgsStyle,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QFontDatabase, QImage, QPainter


def load_fonts(dirs):
    """Polices TrueType des symboles et des étiquettes, mises à disposition de QGIS."""
    for d in dirs:
        for f in Path(d).glob("*.[tT][tT][fF]"):
            QFontDatabase.addApplicationFont(str(f))


def xml_layer(gpkg, name, style):
    """Couche de la grille, en catégories « symbole » : symbole du style XML de même nom (code absent : non dessiné)."""
    layer = QgsVectorLayer(f"{gpkg}|layername={name}", name, "ogr")
    names = set(style.symbolNames()) if style else set()
    values = sorted(str(v) for v in layer.uniqueValues(layer.fields().indexOf("symbole")))
    cats = [QgsRendererCategory(v, style.symbol(v), v) for v in values if v in names]
    layer.setRenderer(QgsCategorizedSymbolRenderer("symbole", cats))
    return layer


def style_symbols(path):
    """Bibliothèque de styles QGIS (XML) → QgsStyle, ou None."""
    if not path or not Path(path).is_file():
        return None
    style = QgsStyle()
    style.createMemoryDatabase()
    if not style.importXml(str(path)):
        print(f"  ⚠ {path}: bibliothèque de styles illisible")
        return None
    return style


def main():
    jobs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    app = QgsApplication([], False)
    app.initQgis()
    load_fonts(jobs[0].get("fonts", []) if jobs else [])
    styles = {}  # chemin du XML → QgsStyle
    for job in jobs:
        if job.get("style") not in styles:
            styles[job.get("style")] = style_symbols(job.get("style"))
        style = styles[job.get("style")]
        layers = [xml_layer(job["gpkg"], job["layer"], style)]
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
