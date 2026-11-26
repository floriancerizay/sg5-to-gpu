"""
Conversion QML CNIG → symboliseurs SLD 1.0 GeoServer (préfixe sld:), sans dépendance externe.

Couvre les couches présentes dans les QML CNIG, en reproduisant le rendu QGIS à 0,28 mm/px :

  SimpleLine        → LineSymbolizer (couleur, épaisseur, extrémités, tireté personnalisé ou style Qt
                      dash / dot / dash dot, décalage du tireté, décalage latéral)
  MarkerLine        → LineSymbolizer / GraphicStroke (picto, pas = intervalle, angle et décalage du picto,
                      décalage latéral) ; placement « CentralPoint » non traduit (signalé ; seuls cas CNIG désactivés)
  SimpleFill        → PolygonSymbolizer (remplissage plein ou aucun, contour)
  PointPatternFill  → PolygonSymbolizer / GraphicFill (pas → graphic-margin, origine de la trame et décalage
                      du picto → Displacement)
  LinePatternFill   → PolygonSymbolizer / GraphicFill shape:// (pas, angle, décalage, trait du sous-symbole)
  CentroidFill      → PointSymbolizer (GeoServer place le picto au centroïde du polygone)
  SimpleMarker      → Mark (circle, square, triangle, star, cross, x ; diamond = carré tourné de 45° ;
                      line = shape://vertline), contour aux valeurs par défaut de QGIS
  SvgMarker         → ExternalGraphic (SVG embarqué → data/svg, nommé comme le SVG CNIG de même contenu ;
                      la taille QGIS est la largeur, la taille SLD la hauteur)
  FontMarker        → Mark ttf://Police#0xNN
  EllipseMarker     → SVG généré (rectangle, ellipse)

Conventions : Displacement en y vers le bas, comme QGIS (mesuré sur GeoServer : DisplacementY > 0 descend
le picto, contrairement à la spécification SE) ; contours de polygone
décalés vers l'intérieur chez QGIS, vers l'extérieur chez GeoServer (signe inversé pour les symboles
« fill ») ; couches désactivées ignorées.

Sortie de Converter.symbolizers : liste d'éléments lxml, à compléter par pattern_tiles (tuiles de trame,
pictos de ligne). Les pas des pictos de ligne (pitches) et les remarques (notes) sont renvoyés à part.
"""

import base64
import hashlib
import math
import re
import urllib.parse
from pathlib import Path
from typing import ClassVar

from lxml import etree

from sld import NS_SLD, NS_XLINK

DPI = 90.714  # 0,28 mm/px : résolution de rendu GeoServer (et de la référence QGIS)
UNIT = {"Point": DPI / 72, "MM": DPI / 25.4, "Pixel": 1.0, "Inch": DPI}
MIN_STROKE = 0.7  # px : en dessous, GeoServer n'affiche quasiment plus le trait
# styles de trait Qt prédéfinis (motif en multiples de l'épaisseur, comme QPen)
QT_DASHES = {"dash": (4, 2), "dot": (1, 2), "dash dot": (4, 2, 1, 2), "dash dot dot": (4, 2, 1, 2, 1, 2)}
QT_CAPS = {"round": "round", "square": "square", "flat": "butt"}
QT_JOINS = {"round": "round", "bevel": "bevel", "miter": "mitre"}
MARKS = {
    "circle": "circle",
    "square": "square",
    "rectangle": "square",
    "triangle": "triangle",
    "equilateral_triangle": "triangle",
    "star": "star",
    "cross": "cross",
    "cross2": "x",
    "line": "shape://vertline",
}
QGIS_OUTLINE = "35,35,35,255"  # couleur de contour par défaut de QGIS
JITTER = "{urn:test-symbo}jitter"  # attribut interne : décalage aléatoire des points d'une trame (pattern_tiles)


# --- lecture du QML ------------------------------------------------------------------------------


def px(value, unit):
    try:
        return float(value) * UNIT.get(unit or "MM", 1.0)
    except (TypeError, ValueError):
        return None


def props(layer):
    """Options de premier niveau d'une couche QML (pas celles des sous-symboles)."""
    opt = layer.find("Option")
    if opt is None:
        return {}
    return {o.get("name"): o.get("value") for o in opt.findall("Option") if o.get("name")}


def active(layer):
    """Couche dessinée : activée et, si elle porte un sous-symbole, au moins une sous-couche activée
    (ex. point blanc désactivé de PSC-L_07-00_001)."""
    if layer.get("enabled", "1") == "0":
        return False
    subs = layer.findall("symbol/layer")
    return not subs or any(active(sub) for sub in subs)


def qml_color(value):
    """« r,g,b,a[,rgb:…] » → (#rrggbb, opacité)."""
    try:
        r, g, b, a = (int(v) for v in (value or "").split(",")[:4])
    except ValueError:
        return None, 1.0
    return f"#{r:02x}{g:02x}{b:02x}", a / 255


def dash_of(p):
    """stroke-dasharray d'une SimpleLine : tireté personnalisé, ou style Qt prédéfini (ex. EL3 « dash dot »)."""
    if p.get("use_custom_dash") not in ("1", "true"):
        pattern = QT_DASHES.get((p.get("line_style") or "solid").lower())
        if not pattern:
            return None
        w = max(1.0, px(p.get("line_width") or 0.26, p.get("line_width_unit")) or 1.0)
        return " ".join(f"{v * w:g}" for v in pattern)
    vals = [px(v, p.get("customdash_unit")) for v in (p.get("customdash") or "").split(";") if v]
    return " ".join(f"{v:g}" for v in vals if v is not None) or None


def offset_xy(q):
    """Décalage « x,y » d'un picto QML en px (y vers le bas)."""
    x, _, y = (q.get("offset") or "0,0").partition(",")
    return px(x or 0, q.get("offset_unit")) or 0.0, px(y or 0, q.get("offset_unit")) or 0.0


def svg_aspect(path):
    """Rapport hauteur / largeur d'un fichier SVG (viewBox, sinon width/height), 1 à défaut."""
    head = Path(path).read_text(encoding="utf-8", errors="ignore")[:4000]
    m = re.search(r'viewBox\s*=\s*"\s*([-\d.e]+)[\s,]+([-\d.e]+)[\s,]+([-\d.e]+)[\s,]+([-\d.e]+)', head)
    if m:
        w, h = float(m.group(3)), float(m.group(4))
    else:
        mw = re.search(r'<svg[^>]*\swidth="([\d.]+)', head)
        mh = re.search(r'<svg[^>]*\sheight="([\d.]+)', head)
        if not (mw and mh):
            return 1.0
        w, h = float(mw.group(1)), float(mh.group(1))
    return h / w if w > 0 and h > 0 else 1.0


# --- écriture SLD --------------------------------------------------------------------------------


def _el(parent, tag, text=None, **attrs):
    e = (
        etree.SubElement(parent, f"{{{NS_SLD}}}{tag}", **attrs)
        if parent is not None
        else etree.Element(f"{{{NS_SLD}}}{tag}", **attrs)
    )
    if text is not None:
        e.text = text
    return e


def _css(parent, name, value):
    _el(parent, "CssParameter", value, name=name)


def _num(v):
    return f"{v:.4g}"


class Converter:
    """Conversion des symboles QML d'un domaine (plu, psmv, sup) ; écrit les SVG dans svg_dir."""

    def __init__(self, svg_dir, domain, svg_names):
        self.svg_dir, self.domain = Path(svg_dir), domain
        self.svg_names = svg_names  # sha1 du contenu → nom du SVG CNIG

    # pictos -------------------------------------------------------------------------------------

    def _svg_file(self, data, stem="qml"):
        """Écrit un SVG ; nom du SVG CNIG de même contenu (préfixé psmv_ en PSMV), sinon <stem>_<sha1>.svg."""
        sha = hashlib.sha1(data).hexdigest()
        cnig = self.svg_names.get(sha, {})
        cnig_name = cnig.get(self.domain) or next(iter(cnig.values()), None)
        if cnig_name is None:
            name = f"{stem}_{sha[:12]}.svg"
        elif self.domain == "psmv":
            name = f"psmv_{cnig_name}"
        else:
            name = cnig_name
        path = self.svg_dir / name
        if not path.exists():
            path.write_bytes(data)
        return name

    def graphic(self, layer, notes):
        """Graphic SLD d'un picto QML (SimpleMarker, SvgMarker, FontMarker, EllipseMarker) → (Graphic, largeur px)
        ou (None, 0) si non traduisible."""
        cls, q = layer.get("class"), props(layer)
        size = px(q.get("size") or 2, q.get("size_unit")) or 0.0
        color, alpha = qml_color(q.get("color"))
        g = _el(None, "Graphic")
        angle = float(q.get("angle") or 0)
        width = height = size
        if cls == "SimpleMarker":
            name = q.get("name") or "circle"
            wkn = MARKS.get(name)
            if name == "diamond":  # carré tourné : la diagonale vaut la taille QGIS
                wkn, width, height, angle = "square", size / math.sqrt(2), size / math.sqrt(2), angle + 45
            if wkn is None:
                notes.append(f"⚠ marque QGIS « {name} » non traduite (carré à la place)")
                wkn = "square"
            mark = _el(g, "Mark")
            _el(mark, "WellKnownName", wkn)
            if not wkn.startswith("shape://") and color and alpha > 0:
                fill = _el(mark, "Fill")
                _css(fill, "fill", color)
                if alpha < 1:
                    _css(fill, "fill-opacity", f"{alpha:.2f}")
            self._outline(mark, q, cls)
        elif cls == "FontMarker":
            chars = q.get("chr") or ""
            if len(chars) != 1:
                notes.append(f"⚠ caractère de police « {chars} » non traduit")
                return None, 0.0
            mark = _el(g, "Mark")
            _el(mark, "WellKnownName", f"ttf://{q.get('font') or 'Arial'}#0x{ord(chars):X}")
            fill = _el(mark, "Fill")
            _css(fill, "fill", color or "#000000")
            if alpha < 1:
                _css(fill, "fill-opacity", f"{alpha:.2f}")
            self._outline(mark, q, cls)
        elif cls in ("SvgMarker", "EllipseMarker"):
            if cls == "SvgMarker":
                name = q.get("name") or ""
                if not name.startswith("base64:"):
                    notes.append(f"⚠ picto SVG externe « {name} » non embarqué dans le QML")
                    return None, 0.0
                fname = self._svg_file(base64.b64decode(name[7:]))
            else:
                fname = self._ellipse_svg(q, notes)
                if fname is None:
                    return None, 0.0
            width = size
            if cls == "EllipseMarker":
                width = px(q.get("symbol_width") or q.get("size"), q.get("symbol_width_unit")) or size
            height = width * svg_aspect(self.svg_dir / fname)  # QGIS : largeur ; SLD : hauteur
            params = {"fill": color or "#000000"}
            if alpha < 1:
                params["fill-opacity"] = f"{alpha:.2f}"
            oc, _ = qml_color(q.get("outline_color") or QGIS_OUTLINE)
            ow = px(q.get("outline_width") or 0, q.get("outline_width_unit")) or 0.0
            if oc and ow > 0:
                params.update({"outline": oc, "outline-width": f"{ow:.3g}"})
            eg = _el(g, "ExternalGraphic")
            _el(
                eg,
                "OnlineResource",
                **{f"{{{NS_XLINK}}}href": f"{fname}?{urllib.parse.urlencode(params)}", f"{{{NS_XLINK}}}type": "simple"},
            )
            _el(eg, "Format", "image/svg+xml")
        else:
            notes.append(f"⚠ picto QML {cls} non traduit")
            return None, 0.0
        _el(g, "Size", _num(height))
        if angle % 360:
            _el(g, "Rotation", _num(angle % 360))
        ox, oy = offset_xy(q)
        if abs(ox) > 0.05 or abs(oy) > 0.05:
            d = _el(g, "Displacement")
            _el(d, "DisplacementX", f"{ox:.2f}")
            _el(d, "DisplacementY", f"{oy:.2f}")  # GeoServer comme QGIS : y vers le bas (mesuré)
        return g, width

    def _outline(self, mark, q, cls):
        """Contour d'une marque, valeurs par défaut de QGIS : #232323 ; épaisseur 0 = trait fin pour une
        SimpleMarker, pas de contour pour une FontMarker."""
        color, alpha = qml_color(q.get("outline_color") or QGIS_OUTLINE)
        width = px(q.get("outline_width") or 0, q.get("outline_width_unit")) or 0.0
        if q.get("outline_style", "solid") == "no" or not color or (cls == "FontMarker" and width <= 0):
            return
        stroke = _el(mark, "Stroke")
        _css(stroke, "stroke", color)
        _css(stroke, "stroke-width", f"{width if width > 0 else 1:.3g}")
        if alpha < 1:
            _css(stroke, "stroke-opacity", f"{alpha:.2f}")

    def _ellipse_svg(self, q, notes):
        name = q.get("symbol_name") or "circle"
        w = px(q.get("symbol_width") or q.get("size") or 2, q.get("symbol_width_unit")) or 1.0
        h = px(q.get("symbol_height") or q.get("size") or 2, q.get("symbol_height_unit")) or 1.0
        oc, _ = qml_color(q.get("outline_color") or QGIS_OUTLINE)
        ow = px(q.get("outline_width") or 0, q.get("outline_width_unit")) or 0.0
        fc, fa = qml_color(q.get("color"))
        style = (
            f'fill="{fc if fa > 0 else "none"}" fill-opacity="{fa:.2f}" stroke="{oc}" '
            f'stroke-width="{ow if ow > 0 else 1:.3g}"'
        )
        m = ow / 2 if ow > 0 else 0.5
        vb = f"{-m:.3f} {-m:.3f} {w + 2 * m:.3f} {h + 2 * m:.3f}"
        if name == "rectangle":
            shape = f'<rect x="0" y="0" width="{w:.3f}" height="{h:.3f}" {style}/>'
        elif name in ("circle", "ellipse"):
            shape = f'<ellipse cx="{w / 2:.3f}" cy="{h / 2:.3f}" rx="{w / 2:.3f}" ry="{h / 2:.3f}" {style}/>'
        else:
            notes.append(f"⚠ ellipse QGIS « {name} » non traduite")
            return None
        data = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w + 2 * m:.3f}" height="{h + 2 * m:.3f}" '
            f'viewBox="{vb}">{shape}</svg>'
        ).encode()
        return self._svg_file(data, "ellipse")

    # couches ------------------------------------------------------------------------------------

    # classe de couche QML → méthode de conversion
    LAYER_CONVERTERS: ClassVar[dict] = {
        "SimpleLine": "_simple_line",
        "MarkerLine": "_marker_line",
        "SimpleFill": "_simple_fill",
        "PointPatternFill": "_point_pattern_fill",
        "LinePatternFill": "_line_pattern_fill",
        "CentroidFill": "_centroid_fill",
        "SimpleMarker": "_point",
        "SvgMarker": "_point",
        "FontMarker": "_point",
        "EllipseMarker": "_point",
    }

    def symbolizers(self, symbol, notes, pitches):
        """Symboliseurs SLD d'un <symbol> QML, dans l'ordre de dessin (ordre des couches du QML)."""
        fill_sign = -1 if symbol.get("type") == "fill" else 1
        out = []
        for layer in symbol.findall("layer"):
            if not active(layer):
                continue
            cls = layer.get("class")
            method = self.LAYER_CONVERTERS.get(cls)
            if method is None:
                notes.append(f"⚠ couche QML {cls} non traduite")
                continue
            out += getattr(self, method)(layer, notes, pitches, fill_sign)
        return out

    def _simple_line(self, layer, notes, pitches, fill_sign):
        p = props(layer)
        if (p.get("line_style") or "solid") == "no":
            return []
        color, alpha = qml_color(p.get("line_color") or p.get("color"))
        width = px(p.get("line_width") or 0.26, p.get("line_width_unit")) or 0.0
        sym = _el(None, "LineSymbolizer")
        stroke = _el(sym, "Stroke")
        _css(stroke, "stroke", color or "#000000")
        _css(stroke, "stroke-width", f"{max(width, MIN_STROKE) if width > 0 else 1:.4g}")
        if alpha < 1:
            _css(stroke, "stroke-opacity", f"{alpha:.2f}")
        _css(stroke, "stroke-linecap", QT_CAPS.get(p.get("capstyle"), "square"))
        _css(stroke, "stroke-linejoin", QT_JOINS.get(p.get("joinstyle"), "bevel"))
        dash = dash_of(p)
        if dash:
            _css(stroke, "stroke-dasharray", dash)
            off = px(p.get("dash_pattern_offset") or 0, p.get("dash_pattern_offset_unit")) or 0.0
            period = sum(float(v) for v in dash.split())
            if abs(off) > 0.05 and period > 0:
                _css(stroke, "stroke-dashoffset", f"{off % period:.2f}")
        self._perpendicular(sym, p, fill_sign)
        return [sym]

    def _perpendicular(self, sym, p, fill_sign):
        """Décalage latéral (QGIS : vers l'intérieur d'un polygone ; GeoServer : vers l'extérieur)."""
        off = (px(p.get("offset") or 0, p.get("offset_unit")) or 0.0) * fill_sign
        if abs(off) > 0.05:
            _el(sym, "PerpendicularOffset", f"{off:.2f}")

    def _marker_line(self, layer, notes, pitches, fill_sign):
        p = props(layer)
        placement = p.get("placements") or p.get("placement") or "Interval"
        if placement.lower() != "interval":
            notes.append(f"⚠ ligne de pictos à placement « {placement} » non traduite")
            return []
        interval = px(p.get("interval") or 3, p.get("interval_unit")) or 1.0
        along = px(p.get("offset_along_line") or 0, p.get("offset_along_line_unit")) or 0.0
        out = []
        for sub in layer.findall("symbol/layer"):
            if not active(sub):
                continue
            g, w = self.graphic(sub, notes)
            if g is None:
                continue
            sym = _el(None, "LineSymbolizer")
            stroke = _el(sym, "Stroke")
            _el(stroke, "GraphicStroke").append(g)
            _css(stroke, "stroke-dasharray", f"{w:.2f} {max(0.5, interval - w):.2f}")
            if abs(along) > 0.05:
                _css(stroke, "stroke-dashoffset", f"{-along:.2f}")  # ramené dans [0, période) par pattern_tiles
            self._perpendicular(sym, p, fill_sign)
            pitches[stroke] = interval  # pas exact, appliqué après intégration de la rotation
            out.append(sym)
        return out

    def _simple_fill(self, layer, notes, pitches, fill_sign):
        p = props(layer)
        style, ostyle = p.get("style") or "solid", p.get("outline_style") or "solid"
        color, alpha = qml_color(p.get("color"))
        if style not in ("solid", "no"):
            notes.append(f"⚠ remplissage QGIS « {style} » non traduit (aplat à la place)")
        sym = _el(None, "PolygonSymbolizer")
        if style != "no" and color and alpha > 0:
            fill = _el(sym, "Fill")
            _css(fill, "fill", color)
            if alpha < 1:
                _css(fill, "fill-opacity", f"{alpha:.2f}")
        if ostyle != "no":
            oc, oa = qml_color(p.get("outline_color") or QGIS_OUTLINE)
            ow = px(p.get("outline_width") or 0.26, p.get("outline_width_unit")) or 0.0
            stroke = _el(sym, "Stroke")
            _css(stroke, "stroke", oc or "#000000")
            _css(stroke, "stroke-width", f"{max(ow, MIN_STROKE) if ow > 0 else 1:.4g}")
            if oa < 1:
                _css(stroke, "stroke-opacity", f"{oa:.2f}")
            _css(stroke, "stroke-linejoin", QT_JOINS.get(p.get("joinstyle"), "bevel"))
            dash = QT_DASHES.get(ostyle)
            if dash:
                _css(stroke, "stroke-dasharray", " ".join(f"{v * max(ow, 1):g}" for v in dash))
        return [sym] if len(sym) else []

    def _point_pattern_fill(self, layer, notes, pitches, fill_sign):
        p = props(layer)
        dx = px(p.get("distance_x") or 15, p.get("distance_x_unit")) or 1.0
        dy = px(p.get("distance_y") or 15, p.get("distance_y_unit")) or 1.0
        out = []
        for sub in layer.findall("symbol/layer"):
            if not active(sub):
                continue
            g, w = self.graphic(sub, notes)
            if g is None:
                continue
            h = float(g.find(f"{{{NS_SLD}}}Size").text)
            # picto-texture qui déborde légèrement du pas : recouvrements / bandes, aussi sous QGIS (INF-S_09)
            if any(b and 0.02 < (a - b) / b < 0.25 for a, b in ((w, dx), (h, dy))):
                notes.append(
                    f"⚠ motif presque jointif : picto {w / UNIT['Point']:.1f} × {h / UNIT['Point']:.1f} pt pour "
                    f"un pas de {dx / UNIT['Point']:.1f} × {dy / UNIT['Point']:.1f} pt (bandes ou "
                    f"recouvrements, aussi sous QGIS)"
                )
            # origine de la trame par rapport au centrage par défaut (50 % du pas), y vers le bas
            sx, sy = _pattern_offset(p, "x", dx), _pattern_offset(p, "y", dy)
            if abs(sx) > 0.05 or abs(sy) > 0.05:
                d = g.find(f"{{{NS_SLD}}}Displacement")
                if d is None:
                    d = _el(g, "Displacement")
                    _el(d, "DisplacementX", "0")
                    _el(d, "DisplacementY", "0")
                ex, ey = d.find(f"{{{NS_SLD}}}DisplacementX"), d.find(f"{{{NS_SLD}}}DisplacementY")
                ex.text = f"{float(ex.text) + sx:.2f}"
                ey.text = f"{float(ey.text) + sy:.2f}"
            sym = _el(None, "PolygonSymbolizer")
            _el(_el(sym, "Fill"), "GraphicFill").append(g)
            mx, my = (dx - w) / 2, (dy - h) / 2  # marge négative possible : pas exact dans la tuile
            _el(sym, "VendorOption", f"{my:.4g} {mx:.4g} {my:.4g} {mx:.4g}", name="graphic-margin")
            # décalage aléatoire de chaque point (QGIS random_deviation, en % du pas ou en unités) : INF-S_36
            jx = _deviation(p, "x", dx)
            jy = _deviation(p, "y", dy)
            if jx > 0.05 or jy > 0.05:
                sym.set(JITTER, f"{jx:.3f} {jy:.3f} {p.get('seed') or 0}")
            out.append(sym)
        return out

    def _line_pattern_fill(self, layer, notes, pitches, fill_sign):
        g = hatch_graphic(layer)
        if g is None:
            notes.append("⚠ hachure QGIS (LinePatternFill) non traduite")
            return []
        sym = _el(None, "PolygonSymbolizer")
        _el(_el(sym, "Fill"), "GraphicFill").append(g)
        return [sym]

    def _centroid_fill(self, layer, notes, pitches, fill_sign):
        out = []
        for sub in layer.findall("symbol/layer"):
            if active(sub):
                g, _ = self.graphic(sub, notes)
                if g is not None:
                    sym = _el(None, "PointSymbolizer")
                    sym.append(g)
                    out.append(sym)
        return out

    def _point(self, layer, notes, pitches, fill_sign):
        g, _ = self.graphic(layer, notes)
        if g is None:
            return []
        sym = _el(None, "PointSymbolizer")
        sym.append(g)
        return [sym]


def _deviation(p, axis, dist):
    value, unit = p.get(f"random_deviation_{axis}"), p.get(f"random_deviation_{axis}_unit")
    if value in (None, ""):
        return 0.0
    return float(value) / 100 * dist if unit == "Percentage" else (px(value, unit) or 0.0)


def _pattern_offset(p, axis, dist):
    """Décalage de l'origine d'une PointPatternFill par rapport au centrage par défaut (50 % du pas), en px."""
    value, unit = p.get(f"offset_{axis}"), p.get(f"offset_{axis}_unit")
    if value in (None, "") or not dist:
        return 0.0
    o = float(value) / 100 * dist if unit == "Percentage" else (px(value, unit) or 0.0)
    return (o % dist) - dist / 2


def hatch_graphic(lpf):
    """Graphic SLD d'une LinePatternFill QML : marque shape:// (pas, angle, décalage le long de la normale,
    trait du sous-symbole), ou None si non traduisible."""
    p = props(lpf)
    sub = next((x for x in lpf.findall("symbol/layer") if active(x)), None)
    if sub is None or sub.get("class") != "SimpleLine":
        return None
    q = props(sub)
    angle = float(p.get("angle") or 0) % 180
    dist = px(p.get("distance"), p.get("distance_unit"))
    off = px(p.get("offset") or 0, p.get("offset_unit")) or 0.0
    color, alpha = qml_color(q.get("line_color") or q.get("color"))
    width = px(q.get("line_width"), q.get("line_width_unit"))
    if not dist or color is None:
        return None
    diag = {45.0: "shape://slash", 135.0: "shape://backslash"}
    if angle in diag:
        wkn, size, rot = diag[angle], dist * math.sqrt(2), None
        disp = (off * math.sqrt(2) * (1 if angle == 45 else -1), 0.0)
    elif angle == 90:
        wkn, size, rot, disp = "shape://vertline", dist, None, (off, 0.0)
    else:
        wkn, size, rot, disp = "shape://horline", dist, (None if angle == 0 else -angle), (0.0, -off)
        if rot is not None:  # décalage le long de la normale au trait : −offset·(−sin θ, cos θ)
            disp = (off * math.sin(math.radians(angle)), -off * math.cos(math.radians(angle)))
    g = _el(None, "Graphic")
    mark = _el(g, "Mark")
    _el(mark, "WellKnownName", wkn)
    stroke = _el(mark, "Stroke")
    _css(stroke, "stroke", color)
    _css(stroke, "stroke-width", f"{max(width or MIN_STROKE, MIN_STROKE):g}")
    if alpha < 1:
        _css(stroke, "stroke-opacity", f"{alpha:.2f}")
    dash = dash_of(q)
    if dash:
        _css(stroke, "stroke-dasharray", dash)
    _el(g, "Size", f"{size:.2f}")
    if rot is not None:
        _el(g, "Rotation", f"{rot:g}")
    if abs(disp[0]) > 0.05 or abs(disp[1]) > 0.05:
        d = _el(g, "Displacement")
        _el(d, "DisplacementX", f"{disp[0]:.2f}")
        _el(d, "DisplacementY", f"{-disp[1]:.2f}")  # disp calculé y vers le haut ; GeoServer : y vers le bas
    return g


# --- règles d'un QML -----------------------------------------------------------------------------


def rule_filter(expression):
    """Filtre QGIS des règles d'ASSIETTE_SUP_S → (suptype, {attribut: valeur}) ou None si non reconnu.
    « lower(string_to_array(idass,'-')[0]) = 'a1' [AND typeass = '…' | IN (…)] »."""
    expr = (expression or "").replace('"', "")
    m = re.search(r"string_to_array\(\s*idass\s*,\s*'-'\s*\)\[0\]\)\s*=\s*'([^']+)'", expr)
    if not m:
        return None
    attrs = dict(re.findall(r"(\w+)\s*=\s*'([^']*)'", expr[m.end() :]))
    for name, values in re.findall(r"(\w+)\s+IN\s*\(([^)]*)\)", expr[m.end() :]):
        attrs[name] = re.findall(r"'([^']*)'", values)
    return m.group(1).lower(), attrs


def ogc_conditions(suptype, attrs):
    """Conditions [(attribut, valeur)] d'une règle d'assiette (suptype, puis attributs ; liste IN : 1re valeur)."""
    return [("suptype", suptype)] + [(k, v if isinstance(v, str) else v[0]) for k, v in attrs.items()]
