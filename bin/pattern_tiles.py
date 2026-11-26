"""
Tuiles SVG de remplissage synthétisées, pour contourner des limites du GraphicFill de GeoServer :

1. la Rotation d'une ExternalGraphic est ignorée, celle d'une marque est appliquée dans chaque tuile
   (hachure shape://horline à 30° → tirets au lieu de traits continus) ;
2. chaque image de trame est arrondie au pixel séparément : deux trames superposées de même pas
   (étoile + étoile intérieure) ne restent pas calées ;
3. une tuile de quelques pixels est arrondie grossièrement : une trame de points fins devient un aplat ;
4. une image SVG de GraphicFill est pixellisée à ⌈largeur⌉ × ⌈hauteur⌉ puis entourée d'une bordure
   transparente de 1 px : le pas réel vaut ⌈taille⌉ + 2 + marges (mesuré sur GeoServer 2.27), soit
   des trames de pictos ~2 px trop lâches. graphic-margin = -1 annule cette bordure.

Une trame (Graphic de GraphicFill + graphic-margin) devient un SVG de exactement pas_x × pas_y,
motif centré, rotation, opacité et couleurs intégrées ; les trames consécutives de même pas sont
fusionnées dans une seule tuile ; une tuile trop petite est répétée k × k fois.
Les hachures deviennent une tuile périodique d/|sin θ| × d/|cos θ| traversée par les traits ; les
hachures consécutives de même angle et même pas sont fusionnées (écart entre traits conservé).
Hachure tiretée : horizontale / verticale, tuile d × période du tiret ; oblique hors 45°, les tirets de tous
les traits partent d'une même perpendiculaire (comme QGIS) et la tuile est répétée k fois avec k·w·|cos θ|
et k·h·sin θ multiples de la période du tiret (ajustée à l'échelle près). À 45°, shape://slash|backslash
natif (correct).

Pictogrammes le long d'une ligne (GraphicStroke, stroke_graphics) : rotation intégrée au SVG (rot_<hash>.svg),
décalage reporté en stroke-dashoffset / PerpendicularOffset.

Entrée/sortie : symboliseurs normalisés (sld_normalize), hrefs relatifs « X.svg?… » résolus dans svg_dir.
"""
import hashlib
import math
import random
import re
import subprocess
import urllib.parse
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from lxml import etree

from config import FONTS_DIR
from sld_normalize import NS_OGC, NS_SLD, NS_XLINK, child, children, local

MIN_TILE = 16.0  # px : en dessous, la tuile est répétée k × k fois
MIN_JITTER_TILE = 96.0  # px : tuile d'une trame à points décalés aléatoirement (répétition peu visible)
JITTER = "{urn:test-symbo}jitter"  # attribut interne posé par qml2sld
MAX_TILE = 96.0  # px : borne de la recherche de k
SAME_PITCH = .05  # écart relatif de pas en dessous duquel deux trames superposées sont fusionnées
MAX_DASH_TILE = 320.0  # px : idem pour une hachure tiretée oblique (contrainte de période du tiret)
NS_SVG = "http://www.w3.org/2000/svg"
SVG_PARAM_DEFAULTS = {"fill": "#000000", "fill-opacity": "1", "outline": "#232323", "outline-opacity": "1",
                      "outline-width": "0"}


def _f(text, default=0.0):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _child_float(el, name, default=0.0):
    """Valeur numérique de l'enfant `name` de `el` (Size, Rotation…), `default` s'il est absent."""
    c = child(el, name) if el is not None else None
    return _f(c.text if c is not None else None, default)


def _displacement(graphic):
    """(DisplacementX, DisplacementY) d'un Graphic, (0, 0) sans Displacement."""
    disp = child(graphic, "Displacement")
    return _child_float(disp, "DisplacementX"), _child_float(disp, "DisplacementY")


def _css(parent):
    return {c.get("name"): (c.text or "").strip() for c in parent if local(c) in ("CssParameter", "SvgParameter")} \
        if parent is not None else {}


def margins(sym):
    """graphic-margin « h », « v h » ou « haut droite bas gauche » → (haut, droite, bas, gauche)."""
    for vo in children(sym, "VendorOption"):
        if vo.get("name") == "graphic-margin":
            v = [_f(x) for x in (vo.text or "").split()]
            if len(v) == 1:
                return v * 4
            if len(v) == 2:
                return [v[0], v[1], v[0], v[1]]
            if len(v) == 4:
                return v
    return [0.0] * 4


def fill_graphic(sym):
    if local(sym) != "PolygonSymbolizer" or child(sym, "Stroke") is not None:
        return None
    fill = child(sym, "Fill")
    gf = child(fill, "GraphicFill") if fill is not None else None
    if gf is None or len([c for c in fill if local(c) != "GraphicFill"]):
        return None
    return child(gf, "Graphic")


# --- motifs élémentaires (SVG centré sur l'origine) ----------------------------------------------

def _svg_marker(href, height, svg_dir):
    """Image SVG externe → (élément <svg> centré, largeur, hauteur) avec ses param() résolus."""
    name, _, query = href.partition("?")
    path = Path(svg_dir) / name.rsplit("/", 1)[-1]
    if path.suffix.lower() != ".svg" or not path.is_file():
        return None
    root = etree.parse(str(path), etree.XMLParser(remove_comments=True)).getroot()
    vb = [_f(v) for v in (root.get("viewBox") or "").replace(",", " ").split()]
    if len(vb) != 4:
        w, h = _f(root.get("width"), 0), _f(root.get("height"), 0)
        if not (w and h):
            return None
        root.set("viewBox", f"0 0 {w:g} {h:g}")
        vb = [0, 0, w, h]
    params = {**SVG_PARAM_DEFAULTS, **{k: v for k, v in urllib.parse.parse_qsl(query)}}
    for el in root.iter():
        for attr, value in list(el.attrib.items()):
            if value.startswith("param("):
                pname, _, rest = value[6:].partition(")")
                el.set(attr, params.get(pname) or rest.strip() or SVG_PARAM_DEFAULTS.get(pname, "none"))
    width = height * vb[2] / vb[3]
    for k in ("x", "y", "width", "height", "preserveAspectRatio"):
        root.attrib.pop(k, None)
    root.set("x", f"{-width / 2:.4f}")
    root.set("y", f"{-height / 2:.4f}")
    root.set("width", f"{width:.4f}")
    root.set("height", f"{height:.4f}")
    root.set("overflow", "visible")
    return root, width, height


def _paint(mark):
    fill, stroke = _css(child(mark, "Fill")), _css(child(mark, "Stroke"))
    attrs = {"fill": fill.get("fill", "#808080") if child(mark, "Fill") is not None else "none"}
    if "fill-opacity" in fill:
        attrs["fill-opacity"] = fill["fill-opacity"]
    if child(mark, "Stroke") is not None:
        attrs["stroke"] = stroke.get("stroke", "#000000")
        attrs["stroke-width"] = stroke.get("stroke-width", "1")
        if "stroke-opacity" in stroke:
            attrs["stroke-opacity"] = stroke["stroke-opacity"]
    return attrs


_fonts = {}


def font_file(family):
    """Fichier de police d'une famille : data/fonts (polices des symboles), sinon fontconfig si la famille
    trouvée est bien celle demandée (pas de police de substitution)."""
    if family in _fonts:
        return _fonts[family]
    found = None
    for f in sorted(FONTS_DIR.glob("*")):
        try:
            if TTFont(str(f), lazy=True)["name"].getBestFamilyName() == family:
                found = f
                break
        except Exception:
            continue
    if found is None:
        r = subprocess.run(["fc-match", "-f", "%{family}|%{file}", f"{family}:style=Regular"],
                           capture_output=True, text=True)
        fam, _, path = r.stdout.partition("|")
        if r.returncode == 0 and family in fam.split(","):
            found = Path(path)
    _fonts[family] = found
    return found


def glyph_path(family, code, size):
    """Tracé SVG d'un caractère de police à la taille QGIS (taille de police en px), placé comme QGIS :
    centré sur sa chasse, ligne de base à ascent / 2 sous le centre. None si police ou glyphe introuvable."""
    path = font_file(family)
    if path is None:
        return None
    font = TTFont(str(path))
    cmap = {}
    for table in font["cmap"].tables:  # polices symboles (Wingdings) : seule table (3, 0), plage F0xx
        for c, g in table.cmap.items():
            cmap.setdefault(c, g)
    name = cmap.get(code) or cmap.get(0xF000 | code)
    if name is None:
        return None
    k = size / font["head"].unitsPerEm
    advance = font["hmtx"][name][0] * k
    ascent = font["hhea"].ascent * k
    pen = SVGPathPen(font.getGlyphSet())
    font.getGlyphSet()[name].draw(TransformPen(pen, (k, 0, 0, -k, -advance / 2, ascent / 2)))
    return pen.getCommands() or None


def _mark_marker(mark, size):
    """Marque bien connue ou caractère de police → élément SVG centré, ou None si non traduisible."""
    wkn = child(mark, "WellKnownName")
    name = (wkn.text or "square").strip() if wkn is not None else "square"
    r = size / 2
    m = re.match(r"^ttf://(.+)#0x([0-9A-Fa-f]+)$", name)
    if m:
        d = glyph_path(m.group(1), int(m.group(2), 16), size)
        if d is None:
            return None
        el = etree.Element(f"{{{NS_SVG}}}path", d=d)
    elif name == "circle":
        el = etree.Element(f"{{{NS_SVG}}}circle", r=f"{r:.4f}")
    elif name == "square":
        el = etree.Element(f"{{{NS_SVG}}}rect", x=f"{-r:.4f}", y=f"{-r:.4f}", width=f"{size:.4f}", height=f"{size:.4f}")
    elif name == "triangle":
        el = etree.Element(f"{{{NS_SVG}}}polygon", points=f"{-r:.4f},{r:.4f} {r:.4f},{r:.4f} 0,{-r:.4f}")
    elif name == "star":
        pts = []
        for i in range(10):
            rad = r if i % 2 == 0 else r * .382
            a = math.radians(-90 + 36 * i)
            pts.append(f"{rad * math.cos(a):.4f},{rad * math.sin(a):.4f}")
        el = etree.Element(f"{{{NS_SVG}}}polygon", points=" ".join(pts))
    else:
        return None
    for k, v in _paint(mark).items():
        el.set(k, v)
    return el


def point_motif(sym, svg_dir):
    """Trame ponctuelle → {"w", "h", "el"} (motif centré, rotation et opacité intégrées) ou None."""
    g = fill_graphic(sym)
    if g is None:
        return None
    size = _child_float(g, "Size")
    if size <= 0 or child(g, "AnchorPoint") is not None:
        return None
    eg, mark = child(g, "ExternalGraphic"), child(g, "Mark")
    if eg is not None:
        res = child(eg, "OnlineResource")
        m = _svg_marker(res.get(f"{{{NS_XLINK}}}href") or "", size, svg_dir) if res is not None else None
        if m is None:
            return None
        el, w, h = m
    elif mark is not None:
        wkn = child(mark, "WellKnownName")
        if wkn is not None and (wkn.text or "").startswith("shape://"):
            return None
        el = _mark_marker(mark, size)
        if el is None:
            return None
        w = h = size
    else:
        return None
    top, right, bottom, left = margins(sym)
    group = etree.Element(f"{{{NS_SVG}}}g")
    rot = _child_float(g, "Rotation") % 360
    if rot:
        group.set("transform", f"rotate({rot:g})")
    opacity = _child_float(g, "Opacity", 1)
    if opacity < 1:
        group.set("opacity", f"{opacity:g}")
    group.append(el)
    # QGIS centre le motif (non tourné) dans la cellule ; marges asymétriques et Displacement (y vers le bas,
    # comme GeoServer) → décalage du centre
    ddx, ddy = _displacement(g)
    shift = ((left - right) / 2 + ddx, (top - bottom) / 2 + ddy)
    if w + left + right < .5 or h + top + bottom < .5:
        return None  # pas nul ou négatif : rendu natif
    jitter = [float(v) for v in (sym.get(JITTER) or "").split()]
    return {"w": w + left + right, "h": h + top + bottom, "el": group, "shift": shift,
            "jitter": tuple(jitter) if len(jitter) == 3 else None}


# shape:// → (rotation ajoutée, facteur taille de tuile → pas entre traits)
HATCHES = {"shape://horline": (0, 1), "shape://vertline": (90, 1),
           "shape://slash": (-45, 2 ** -.5), "shape://backslash": (45, 2 ** -.5)}


def hatch_motif(sym):
    """Hachure shape:// → {"w", "h", "el", "hatch": angle, "d", "dash", "along"} ou None.

    Tuile périodique d/|sin θ| × d/|cos θ| (d × d pour un trait horizontal ou vertical).
    """
    g = fill_graphic(sym)
    if g is None:
        return None
    mark = child(g, "Mark")
    wkn = child(mark, "WellKnownName") if mark is not None else None
    kind = HATCHES.get((wkn.text or "").strip()) if wkn is not None else None
    if kind is None:
        return None
    rot = (_child_float(g, "Rotation") + kind[0]) % 180
    stroke = _css(child(mark, "Stroke"))
    dash = [_f(v) for v in stroke.get("stroke-dasharray", "").replace(",", " ").split()]
    axis = rot % 90 < .01 or rot % 90 > 89.99
    if dash:
        # tiret le long d'un trait à 45° : GeoServer le rend correctement en natif
        if (not axis and abs(rot % 90 - 45) < .01) or min(dash) < 0 or sum(dash) <= 0:
            return None
        dash = dash * 2 if len(dash) % 2 else dash
    d = _child_float(g, "Size") * kind[1]
    if d <= 0:
        return None
    a = math.radians(rot)
    sin, cos = abs(math.sin(a)), abs(math.cos(a))
    # le long d'un trait horizontal / vertical, la tuile vaut la période du tiret (d sans tiret)
    w = d / sin if sin > 1e-3 else (sum(dash) if dash else d)
    h = d / cos if cos > 1e-3 else (sum(dash) if dash else d)
    dx, dy = _displacement(g)
    group = etree.Element(f"{{{NS_SVG}}}g", fill="none", stroke=stroke.get("stroke", "#000000"))
    group.set("stroke-width", stroke.get("stroke-width", "1"))
    if "stroke-opacity" in stroke:
        group.set("stroke-opacity", stroke["stroke-opacity"])
    return {"w": w, "h": h, "el": group, "shift": (dx, dy), "hatch": a, "d": d, "dash": dash or None,
            "along": (w * cos, h * sin) if dash and not axis else None, "axis": axis}


# --- tuile ---------------------------------------------------------------------------------------

def repeat(w, h, along=None, period=None, min_tile=MIN_TILE):
    """k tel que la tuile k·w × k·h dépasse MIN_TILE et tombe au plus près de pixels entiers.

    GeoServer pixellise la tuile à une taille entière : sans cela, l'écart d'arrondi laisse une
    couture blanche (ou un chevauchement) à chaque raccord. Hachure tiretée oblique : k·along[0] et
    k·along[1] (glissement le long du trait d'une tuile à la suivante) doivent de plus être des
    multiples de la période du tiret.
    """
    def frac(v):
        return abs(v - round(v))

    k0 = max(1, math.ceil(min_tile / min(w, h)))
    k1 = max(k0, int((MAX_DASH_TILE if along else max(MAX_TILE, 2 * min_tile)) / max(w, h)))
    return min(range(k0, k1 + 1), key=lambda k: (
        frac(k * w) + frac(k * h) + (sum(frac(k * a / period) for a in along) * period if along else 0), k))


def tile_svg(motifs):
    """SVG d'une tuile w × h (répétée k × k si petite) contenant les motifs centrés, dans l'ordre.
    → (octets, hauteur en px entiers). Le contenu est légèrement mis à l'échelle sur des px entiers."""
    # hachures horizontales / verticales fusionnées : la dimension le long du trait est celle du tireté
    first = next((m for m in motifs if m.get("dash")), motifs[0])
    w, h = first["w"], first["h"]
    dashed = next((m for m in motifs if m.get("along")), None)
    jittered = any(m.get("jitter") for m in motifs)
    k = repeat(w, h, dashed["along"], sum(dashed["dash"])) if dashed else \
        repeat(w, h, min_tile=MIN_JITTER_TILE if jittered else MIN_TILE)
    W, H = w * k, h * k
    svg = etree.Element(f"{{{NS_SVG}}}svg", nsmap={None: NS_SVG, "xlink": NS_XLINK},
                        width=str(max(1, round(W))), height=str(max(1, round(H))),
                        viewBox=f"0 0 {W:.4f} {H:.4f}", preserveAspectRatio="none")
    for m in motifs:
        if m.get("hatch") is not None:
            # traits de direction u (rotation SLD horaire = rotation SVG, axe y vers le bas), espacés de d
            # selon la normale ; le trait n passe par (dx, dy) + n·d·normale et traverse toute la tuile
            g = _copy(m["el"])
            ux, uy = math.cos(m["hatch"]), math.sin(m["hatch"])
            d, dash = m["d"], m["dash"]
            if dash and m["along"]:  # oblique : période du tiret ajustée au mieux sur k·along[0] et k·along[1]
                p = sum(dash)
                n_periods = sum(max(1, round(k * a / p)) for a in m["along"])
                dash = [v * k * sum(m["along"]) / (n_periods * p) for v in dash]
            if dash:
                g.set("stroke-dasharray", " ".join(f"{v:.4f}" for v in dash))
            length = 2 * (W + H)
            span = math.ceil((W + H) / d) + 2
            for n in range(-span, span + 1):
                x0, y0 = m["shift"][0] - n * d * uy, m["shift"][1] + n * d * ux
                line = etree.SubElement(g, f"{{{NS_SVG}}}line", x1=f"{x0 - ux * length:.4f}", y1=f"{y0 - uy * length:.4f}",
                                        x2=f"{x0 + ux * length:.4f}", y2=f"{y0 + uy * length:.4f}")
                if dash:  # même phase pour tous les traits au pied de la perpendiculaire (x0, y0)
                    line.set("stroke-dashoffset", f"{-length % sum(dash):.4f}")
            svg.append(g)
            continue
        # décalage aléatoire par cellule (reproductible), identique pour les copies voisines (tuile raccordable)
        jx, jy, seed = m.get("jitter") or (0, 0, 0)
        rng = random.Random(int(seed))
        dev = [[(rng.uniform(-jx, jx), rng.uniform(-jy, jy)) for _ in range(k)] for _ in range(k)]
        # copies voisines : un motif plus grand que la cellule déborde comme chez QGIS
        for i in range(-1, k + 1):
            for j in range(-1, k + 1):
                ddx, ddy = dev[i % k][j % k]
                cx, cy = (i + .5) * w + m["shift"][0] + ddx, (j + .5) * h + m["shift"][1] + ddy
                g = etree.SubElement(svg, f"{{{NS_SVG}}}g", transform=f"translate({cx:.4f},{cy:.4f})")
                g.append(_copy(m["el"]))
    return etree.tostring(svg, xml_declaration=True, encoding="UTF-8"), max(1, round(H))


def _copy(el):
    return etree.fromstring(etree.tostring(el))


def _replace_graphic(sym, href, size):
    g = fill_graphic(sym)
    for c in list(g):
        g.remove(c)
    eg = etree.SubElement(g, f"{{{NS_SLD}}}ExternalGraphic")
    etree.SubElement(eg, f"{{{NS_SLD}}}OnlineResource", {f"{{{NS_XLINK}}}href": href, f"{{{NS_XLINK}}}type": "simple"})
    etree.SubElement(eg, f"{{{NS_SLD}}}Format").text = "image/svg+xml"
    etree.SubElement(g, f"{{{NS_SLD}}}Size").text = str(size)
    for vo in children(sym, "VendorOption"):
        if vo.get("name") == "graphic-margin":
            sym.remove(vo)
    etree.SubElement(sym, f"{{{NS_SLD}}}VendorOption", name="graphic-margin").text = "-1"


def synthesize(syms, svg_dir):
    """Remplace les trames problématiques par des tuiles SVG (écrites dans svg_dir). → (syms, nb tuiles)."""
    out, n, i = [], 0, 0
    while i < len(syms):
        m = point_motif(syms[i], svg_dir) or hatch_motif(syms[i])
        if m is None:
            syms[i].attrib.pop(JITTER, None)
            for vo in children(syms[i], "VendorOption"):
                # marge négative (picto plus grand que le pas) : seulement sûre dans une tuile ; ailleurs
                # GeoServer peut obtenir une tuile de taille nulle et planter
                if vo.get("name") == "graphic-margin" and any(_f(v) < 0 for v in (vo.text or "").split()) \
                        and vo.text.strip() != "-1":
                    vo.text = " ".join(f"{max(0.0, _f(v)):g}" for v in vo.text.split())
            out.append(syms[i])
            i += 1
            continue
        group, j = [m], i + 1
        while j < len(syms):
            if m.get("hatch") is not None:
                nxt = hatch_motif(syms[j])
                if nxt is None or abs(nxt["hatch"] - m["hatch"]) > 1e-3 or abs(nxt["d"] - m["d"]) > .05:
                    break
                if nxt.get("dash") and any(x.get("dash") for x in group):
                    break  # un seul tireté par tuile
                if not m["axis"] and (abs(nxt["w"] - m["w"]) > .05 or abs(nxt["h"] - m["h"]) > .05):
                    break
            else:
                nxt = point_motif(syms[j], svg_dir)
                if nxt is not None and (nxt.get("jitter") or m.get("jitter")):
                    break  # trame à points décalés : tuile à part
                # pas presque égaux (SLD CNIG : 29 / 30 px pour les deux trames de PSC-S_01) : même tuile, pas du 1er
                if nxt is None or abs(nxt["w"] - m["w"]) > SAME_PITCH * m["w"] or abs(nxt["h"] - m["h"]) > SAME_PITCH * m["h"]:
                    break
            group.append(nxt)
            j += 1
        for sym in syms[i:j]:
            sym.attrib.pop(JITTER, None)
        data, size = tile_svg(group)
        name = f"pat_{hashlib.sha1(data).hexdigest()[:12]}.svg"
        (Path(svg_dir) / name).write_bytes(data)
        _replace_graphic(syms[i], name, size)
        out.append(syms[i])
        n += 1
        i = j
    return out, n


# --- pictogrammes répétés le long d'une ligne (GraphicStroke) -----------------------------------

def _css_el(stroke, name):
    return next((c for c in stroke if local(c) in ("CssParameter", "SvgParameter") and c.get("name") == name), None)


def stroke_graphics(syms, svg_dir, surface=False, pitches=None, geometry=None):
    """GraphicStroke : GeoServer ignore la Rotation et applique le Displacement en coordonnées écran
    (picto poussé à côté de la ligne). QGIS tourne picto et décalage avec la ligne. On intègre la rotation
    à une copie du SVG (rot_<hash>.svg) et on reporte le décalage tourné sur la ligne : composante le long
    de la ligne → stroke-dashoffset, composante latérale → PerpendicularOffset ; le tiret qui porte le
    picto est recalé sur sa nouvelle largeur. surface : symbole de polygone (côté du décalage latéral
    inversé, GeoServer réorientant les anneaux). pitches : Stroke → pas voulu (intervalle QML), prioritaire
    sur le tireté écrit. Contour de polygone : picto retourné de 180° (GeoServer l'oriente dans le sens
    inverse de QGIS, alors que le décalage latéral est déjà du bon côté : triangles d'INF-S_04 / 33).
    geometry : nom de l'attribut géométrique ; le picto que QGIS pose au début d'une ligne, que GeoServer
    ne dessine pas (à cheval sur l'extrémité), est ajouté en PointSymbolizer sur startPoint(geometry),
    tourné selon startAngle (PSC-L_07). → nombre de symboliseurs modifiés."""
    n, wanted, starts = 0, {}, []  # Stroke → décalage de tiret voulu (avant bornage)
    for sym in syms:
        if local(sym) != "LineSymbolizer":
            continue
        stroke = child(sym, "Stroke")
        gs = child(stroke, "GraphicStroke") if stroke is not None else None
        g = child(gs, "Graphic") if gs is not None else None
        if g is None:
            continue
        rot_el, disp, size_el = child(g, "Rotation"), child(g, "Displacement"), child(g, "Size")
        rot = _child_float(g, "Rotation") % 360
        dx, dy = _displacement(g)  # y vers le bas (GeoServer, comme QGIS)
        h = _child_float(g, "Size")
        eg = child(g, "ExternalGraphic")
        res = child(eg, "OnlineResource") if eg is not None else None
        marker = _svg_marker(res.get(f"{{{NS_XLINK}}}href") or "", h, svg_dir) if res is not None and h > 0 else None
        if rot and marker is None:
            continue  # marque ou image non SVG : rotation laissée telle quelle
        a = math.radians(rot)
        w = marker[1] if marker else h
        drawn = (rot + (180 if surface else 0)) % 360  # rotation de l'image ; offsets calculés sur rot
        if drawn and marker is not None:
            el, w0, h0 = marker
            W = abs(w0 * math.cos(a)) + abs(h0 * math.sin(a))
            H = abs(w0 * math.sin(a)) + abs(h0 * math.cos(a))
            svg = etree.Element(f"{{{NS_SVG}}}svg", nsmap={None: NS_SVG, "xlink": NS_XLINK}, width=f"{W:.4f}",
                                height=f"{H:.4f}", viewBox=f"{-W / 2:.4f} {-H / 2:.4f} {W:.4f} {H:.4f}")
            grp = etree.SubElement(svg, f"{{{NS_SVG}}}g", transform=f"rotate({drawn:g})")
            grp.append(el)
            data = etree.tostring(svg, xml_declaration=True, encoding="UTF-8")
            name = f"rot_{hashlib.sha1(data).hexdigest()[:12]}.svg"
            (Path(svg_dir) / name).write_bytes(data)
            res.set(f"{{{NS_XLINK}}}href", name)
            size_el.text = f"{H:.4f}"
            if rot_el is not None:
                g.remove(rot_el)
            w_new = W
        else:
            if rot_el is not None:  # rotation nulle (ou 180° sur un contour, compensée)
                g.remove(rot_el)
            w_new = w
        # décalage dans le repère du picto (y vers le bas), tourné avec lui : (le long, à droite de la ligne)
        along = dx * math.cos(a) - dy * math.sin(a)
        right = dx * math.sin(a) + dy * math.cos(a)
        if disp is not None:
            g.remove(disp)
        if abs(right) > .05:
            po = child(sym, "PerpendicularOffset")
            base = _f(po.text if po is not None else None, 0)
            if po is None:
                po = etree.SubElement(sym, f"{{{NS_SLD}}}PerpendicularOffset")
            # PerpendicularOffset > 0 : à gauche du sens de la ligne (spécification SE) ; contour de polygone :
            # vers l'extérieur (GeoServer réoriente les anneaux), QGIS « à droite » = vers l'intérieur
            po.text = f"{base - right:.2f}"
        dash = _css_el(stroke, "stroke-dasharray")
        vals = [_f(v) for v in (dash.text or "").split()] if dash is not None else []
        pitch = (pitches or {}).get(stroke)
        if len(vals) == 2 and (pitch or sum(vals)) - w_new >= .5:  # sinon tireté d'origine (intervalle nul : plantage)
            period = pitch or sum(vals)
            vals = [w_new, period - w_new]
            dash.text = f"{vals[0]:.2f} {vals[1]:.2f}"
        off = _css_el(stroke, "stroke-dashoffset")
        base = _f(off.text if off is not None else None, 0)
        if geometry and not surface and abs(right) < .05 and abs(along) < .05 and abs(base) < .05:
            starts.append((sym, g))
        # QGIS pose le 1er picto au début de la ligne, GeoServer au milieu du 1er tiret (w/2 plus loin)
        along -= w_new / 2 if len(vals) == 2 else 0.0
        wanted[stroke] = base - along
        n += 1
    if wanted:
        _align_dashes(syms, wanted)
    for sym, g in starts:  # picto du début de ligne
        syms.insert(syms.index(sym) + 1, _start_point(g, geometry))
    return n


def _start_point(graphic, geometry):
    """PointSymbolizer sur le 1er sommet d'une ligne, picto tourné selon le 1er segment."""
    point = etree.Element(f"{{{NS_SLD}}}PointSymbolizer")
    geom = etree.SubElement(etree.SubElement(point, f"{{{NS_SLD}}}Geometry"), f"{{{NS_OGC}}}Function", name="startPoint")
    etree.SubElement(geom, f"{{{NS_OGC}}}PropertyName").text = geometry
    g = etree.fromstring(etree.tostring(graphic))
    for old in children(g, "Rotation"):
        g.remove(old)
    rot = etree.SubElement(g, f"{{{NS_SLD}}}Rotation")
    mul = etree.SubElement(rot, f"{{{NS_OGC}}}Mul")  # startAngle : sens trigonométrique ; Rotation : horaire
    angle = etree.SubElement(mul, f"{{{NS_OGC}}}Function", name="startAngle")
    etree.SubElement(angle, f"{{{NS_OGC}}}PropertyName").text = geometry
    etree.SubElement(mul, f"{{{NS_OGC}}}Literal").text = "-1"
    point.append(g)
    return point


def _align_dashes(syms, wanted):
    """Écrit les stroke-dashoffset des tiretés d'un symbole en gardant leurs positions relatives.

    GeoServer refuse un décalage négatif (« negative dash phase ») et GeoTools plante dès que le décalage
    d'un GraphicStroke dépasse l'intervalle de son tireté (ArrayIndexOutOfBounds). On cherche un décalage
    commun δ, appliqué à tous les tiretés du symbole de période égale ou multiple (pictos et traits simples,
    ex. traits bicolores de période 2 × le pas des pictos de PSC-L_07), qui ramène chaque picto dans
    [0, intervalle) ; à défaut, le plus petit dépassement est borné."""
    lines = []  # (Stroke, décalage voulu, intervalle ou None pour un trait simple, période)
    for sym in syms:
        stroke = child(sym, "Stroke") if local(sym) == "LineSymbolizer" else None
        dash = _css_el(stroke, "stroke-dasharray") if stroke is not None else None
        vals = [_f(v) for v in (dash.text or "").split()] if dash is not None else []
        if not vals or sum(vals) <= 0:
            continue
        graphic = child(stroke, "GraphicStroke") is not None
        if graphic and len(vals) != 2:
            continue
        period = sum(vals) * (2 if len(vals) % 2 else 1)
        off = _css_el(stroke, "stroke-dashoffset")
        want = wanted.get(stroke, _f(off.text if off is not None else None, 0))
        lines.append((stroke, want, vals[1] if graphic else None, period))

    def commensurate(a, b):
        r = max(a, b) / min(a, b)
        return abs(r - round(r)) <= .02 * round(r)

    for ref in [l for l in lines if l[0] in wanted]:
        group = [l for l in lines if commensurate(l[3], ref[3])]
        period = max(l[3] for l in group)

        def excess(delta):
            return max((max(0.0, (w + delta) % p - (gap - .01)) for _, w, gap, p in group if gap is not None), default=0)

        steps = [i * period / 800 for i in range(800)]
        delta = min(steps, key=lambda d: (round(excess(d), 3), min(d, period - d)))
        for stroke, w, gap, p in group:
            o = (w + delta) % p
            if gap is not None:
                o = min(o, max(0.0, gap - .01))
            off = _css_el(stroke, "stroke-dashoffset")
            if off is None:
                if abs(o) < .005:
                    continue
                off = etree.SubElement(stroke, f"{{{NS_SLD}}}CssParameter", name="stroke-dashoffset")
            off.text = f"{o:.2f}"
            wanted.pop(stroke, None)
