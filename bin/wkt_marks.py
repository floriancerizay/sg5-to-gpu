"""
Pictos SVG remplacés par des marques natives wkt:// de GeoServer : aucune image à charger.

Un picto d'une seule peinture (remplissage et contour uniformes, cas de tous les pictos CNIG) devient une Mark
dont la forme est le tracé du SVG, courbes aplaties, ramené au centre du viewBox (y vers le haut). Constaté sur
GeoServer 2.28 : la forme d'une marque wkt:// n'est pas recentrée (l'origine tombe sur le point, comme le centre
d'une image) et sa hauteur est mise à Size ; Size vaut donc la Size du SVG × hauteur du tracé / hauteur du viewBox.
Couleurs : paramètres de l'URL (param(fill), param(outline)…), sinon valeurs du SVG. Épaisseur de contour en px :
valeur SVG × Size / hauteur du viewBox.

Le SVG est conservé quand il n'est pas traduisible : peintures multiples, tracé vide ou plat, formes ouvertes et
fermées mêlées, élément inconnu.
"""

import math
import re
import urllib.parse
from pathlib import Path

from fontTools.pens.basePen import BasePen
from fontTools.pens.transformPen import TransformPen
from fontTools.svgLib.path.parser import parse_path
from fontTools.svgLib.path.shapes import PathBuilder
from lxml import etree

from sld import NS_SLD, NS_XLINK, child, local

SVG_PARAMS = {  # valeurs par défaut des param() des SVG CNIG
    "fill": "#000000",
    "fill-opacity": "1",
    "outline": "#232323",
    "outline-opacity": "1",
    "outline-width": "0",
}
PAINT = ("fill", "fill-opacity", "stroke", "stroke-opacity", "stroke-width", "opacity")
SHAPES = {"path", "rect", "circle", "ellipse", "polygon", "polyline", "line"}
CONTAINERS = {"svg", "g", "defs", "title", "desc"}
DIGITS = 4  # décimales des coordonnées WKT (hauteur du viewBox = 1)


class _Flatten(BasePen):
    """Pen qui aplatit les courbes → anneaux [(points, fermé)] (méthodes nommées par fontTools)."""

    def __init__(self, tolerance):
        super().__init__(None)
        self.rings, self.tol = [], tolerance

    def _moveTo(self, p):  # noqa: N802
        self.rings.append([[p], False])

    def _lineTo(self, p):  # noqa: N802
        self.rings[-1][0].append(p)

    def _curveToOne(self, p1, p2, p3):  # noqa: N802
        p0 = self.rings[-1][0][-1]
        length = math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)
        n = max(2, min(24, math.ceil(length / self.tol)))
        for i in range(1, n + 1):
            t = i / n
            u = 1 - t
            self.rings[-1][0].append(
                tuple(
                    u**3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t**3 * d
                    for a, b, c, d in zip(p0, p1, p2, p3, strict=True)
                )
            )

    def _qCurveToOne(self, p1, p2):  # noqa: N802
        p0 = self.rings[-1][0][-1]
        c1 = tuple(a + 2 / 3 * (b - a) for a, b in zip(p0, p1, strict=True))
        c2 = tuple(c + 2 / 3 * (b - c) for b, c in zip(p1, p2, strict=True))
        self._curveToOne(c1, c2, p2)

    def _closePath(self):  # noqa: N802
        self.rings[-1][1] = True

    def _endPath(self):  # noqa: N802
        pass


def _matrix(text):
    """Attribut transform SVG → matrice affine (a, b, c, d, e, f)."""
    m = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text or ""):
        v = [float(x) for x in re.split(r"[\s,]+", args.strip()) if x]
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate":
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate":
            a = math.radians(v[0])
            cx, cy = (v[1], v[2]) if len(v) == 3 else (0, 0)
            cos, sin = math.cos(a), math.sin(a)
            t = (cos, sin, -sin, cos, cx - cos * cx + sin * cy, cy - sin * cx - cos * cy)
        else:
            raise ValueError(name)
        m = _mul(m, t)
    return m


def _mul(m, t):
    """m · t (t appliquée d'abord)."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = t
    return (
        a * a2 + c * b2,
        b * a2 + d * b2,
        a * c2 + c * d2,
        b * c2 + d * d2,
        a * e2 + c * f2 + e,
        b * e2 + d * f2 + f,
    )


def _viewport(el):
    """Transformation d'un <svg> imbriqué (x, y, width, height, viewBox ; xMidYMid meet)."""
    vb = [float(v) for v in (el.get("viewBox") or "").replace(",", " ").split()]
    x, y = float(el.get("x", 0)), float(el.get("y", 0))
    if len(vb) != 4:
        return (1, 0, 0, 1, x, y)
    w, h = float(el.get("width", vb[2])), float(el.get("height", vb[3]))
    s = min(w / vb[2], h / vb[3])
    tx = x + (w - vb[2] * s) / 2 - vb[0] * s
    ty = y + (h - vb[3] * s) / 2 - vb[1] * s
    return (s, 0, 0, s, tx, ty)


def _style(el, inherited):
    st = dict(inherited)
    for k in PAINT:
        if el.get(k) is not None:
            st[k] = el.get(k)
    for decl in (el.get("style") or "").split(";"):
        k, _, v = decl.partition(":")
        if k.strip() in PAINT:
            st[k.strip()] = v.strip()
    return st


def _resolve(value, params):
    """« param(fill) », « param(outline-width) 0 » → valeur du paramètre d'URL, sinon défaut."""
    m = re.match(r"param\(([\w-]+)\)\s*(.*)", (value or "").strip())
    if not m:
        return value
    return params.get(m.group(1)) or m.group(2).strip() or SVG_PARAMS.get(m.group(1))


def svg_shape(path, params):
    """SVG → (anneaux [(points, fermé)] en unités du viewBox, peinture, viewBox) ou None si non traduisible."""
    root = etree.parse(str(path), etree.XMLParser(remove_comments=True)).getroot()
    vb = [float(v) for v in (root.get("viewBox") or "").replace(",", " ").split()]
    if len(vb) != 4 or vb[3] <= 0:
        return None
    flat = _Flatten(vb[3] / 40)
    paints = set()

    def walk(el, m, inherited, top):
        tag = local(el) if isinstance(el.tag, str) else ""
        if not tag:
            return True
        if tag not in SHAPES and tag not in CONTAINERS:
            return False
        st = _style(el, inherited)
        if tag == "svg" and not top:
            m = _mul(m, _viewport(el))
        m = _mul(m, _matrix(el.get("transform")))
        if tag in SHAPES:
            builder = PathBuilder()
            el_copy = etree.fromstring(etree.tostring(el))
            el_copy.attrib.pop("transform", None)
            builder.add_path_from_element(el_copy)
            for d in builder.paths:
                parse_path(d, TransformPen(flat, m))
            paint = tuple(_resolve(st.get(k), params) for k in PAINT)
            paints.add(paint)
        return all(walk(c, m, st, False) for c in el if tag not in ("defs", "title", "desc"))

    try:
        if not walk(root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), {}, True):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    rings = [(pts, closed) for pts, closed in flat.rings if len(pts) >= 2]
    if len(paints) != 1 or not rings:
        return None
    return rings, dict(zip(PAINT, paints.pop(), strict=True)), vb


def _wkt(rings, cx, cy, h):
    """Anneaux → WKT (y vers le haut, centre du viewBox à l'origine, hauteur du viewBox = 1)."""

    def coords(pts):
        return ", ".join(f"{round((x - cx) / h, DIGITS):g} {round((cy - y) / h, DIGITS):g}" for x, y in pts)

    if all(closed for _, closed in rings):
        # GeoServer dimensionne la marque sur l'emprise de la géométrie : celle d'un POLYGON est celle de son
        # anneau extérieur, d'où un MULTIPOLYGON dont chaque morceau a son extérieur (imbrication paire)
        polys = _nest([pts for pts, _ in rings])
        return (
            "MULTIPOLYGON("
            + ", ".join("(" + ", ".join(f"({coords([*ring, ring[0]])})" for ring in poly) + ")" for poly in polys)
            + ")"
        )
    if not any(closed for _, closed in rings):
        return "MULTILINESTRING(" + ", ".join(f"({coords(pts)})" for pts, _ in rings) + ")"
    return None


def _inside(p, ring):
    """Point p dans l'anneau (règle pair-impair)."""
    x, y = p
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _nest(rings):
    """Anneaux → polygones [extérieur, trous…] : un anneau contenu dans un nombre pair d'autres est un extérieur,
    sinon un trou de l'anneau qui le contient au plus près (rendu pair-impair du SVG, comme GeoServer)."""
    contains = [
        [j for j, other in enumerate(rings) if j != i and _inside(ring[0], other)] for i, ring in enumerate(rings)
    ]
    depth = [len(c) for c in contains]
    polys = {i: [rings[i]] for i in range(len(rings)) if depth[i] % 2 == 0}
    for i in range(len(rings)):
        if depth[i] % 2:
            parent = max((j for j in contains[i] if depth[j] == depth[i] - 1), key=lambda j: depth[j], default=None)
            if parent is not None:
                polys[parent].append(rings[i])
    return list(polys.values())


def _paint_ok(value):
    return value not in (None, "", "none")


def to_mark(graphic, svg_dir):
    """Graphic à ExternalGraphic SVG → même Graphic à Mark wkt:// (modifié sur place). → True si converti."""
    eg = child(graphic, "ExternalGraphic")
    res = child(eg, "OnlineResource") if eg is not None else None
    size_el = child(graphic, "Size")
    if res is None or size_el is None:
        return False
    name, _, query = res.get(f"{{{NS_XLINK}}}href", "").partition("?")
    path = Path(svg_dir) / name.rsplit("/", 1)[-1]
    if path.suffix.lower() != ".svg" or not path.is_file():
        return False
    params = dict(urllib.parse.parse_qsl(query.replace("&amp;", "&")))
    shape = svg_shape(path, params)
    if shape is None:
        return False
    rings, paint, vb = shape
    ys = [y for pts, _ in rings for _, y in pts]
    shape_h = max(ys) - min(ys)
    size = float(size_el.text)
    stroke_w = float(paint["stroke-width"] or 1) * size / vb[3] if _paint_ok(paint["stroke"]) else 0.0
    fill = paint["fill"] if _paint_ok(paint["fill"]) else None
    if shape_h < vb[3] * 0.01 or (fill is None and stroke_w <= 0):
        return False
    if fill is not None and stroke_w <= 0:
        rings = [(pts, True) for pts, _ in rings]  # un remplissage SVG ferme les tracés ouverts
    if fill is not None and not all(c for _, c in rings):
        return False  # tracé ouvert rempli et contouré : pas d'équivalent sûr
    wkt = _wkt(rings, vb[0] + vb[2] / 2, vb[1] + vb[3] / 2, vb[3])
    if wkt is None:
        return False
    opacity = float(paint["opacity"] or 1)
    mark = etree.Element(f"{{{NS_SLD}}}Mark")
    etree.SubElement(mark, f"{{{NS_SLD}}}WellKnownName").text = f"wkt://{wkt}"
    if fill is not None:
        f = etree.SubElement(mark, f"{{{NS_SLD}}}Fill")
        etree.SubElement(f, f"{{{NS_SLD}}}CssParameter", name="fill").text = fill
        alpha = float(paint["fill-opacity"] or 1) * opacity
        if alpha < 1:
            etree.SubElement(f, f"{{{NS_SLD}}}CssParameter", name="fill-opacity").text = f"{alpha:g}"
    if stroke_w > 0:
        s = etree.SubElement(mark, f"{{{NS_SLD}}}Stroke")
        etree.SubElement(s, f"{{{NS_SLD}}}CssParameter", name="stroke").text = paint["stroke"]
        etree.SubElement(s, f"{{{NS_SLD}}}CssParameter", name="stroke-width").text = f"{stroke_w:.3f}"
        alpha = float(paint["stroke-opacity"] or 1) * opacity
        if alpha < 1:
            etree.SubElement(s, f"{{{NS_SLD}}}CssParameter", name="stroke-opacity").text = f"{alpha:g}"
    graphic.replace(eg, mark)
    size_el.text = f"{size * shape_h / vb[3]:.4f}"
    return True


def native_marks(syms, svg_dir):
    """Pictos des PointSymbolizer convertis en marques wkt:// → nombre de pictos convertis.

    Pas les pictos le long des lignes (GraphicStroke) : GeoServer y espace et oriente une marque autrement
    qu'une image (mesuré : vagues d'INF-L_19 jointives, flèches de PSC-L_39 tournées d'environ 30°)."""
    return sum(
        to_mark(g, svg_dir)
        for sym in syms
        if local(sym) == "PointSymbolizer" and (g := child(sym, "Graphic")) is not None
    )
