"""
Lecture et écriture de SLD au format des styles de production (STYLES/data/du) : SLD 1.0, préfixes sld: / ogc:.

- lecture : parse, rules, symbolizers, child / children / local (noms sans espace de noms) ;
- écriture : rule_xml, sld_document, to_xml (indentation de 2 espaces, comme la production) ;
- normalize_symbolizer : SLD par code du dépôt CNIG (SE 1.1 écrit par QGIS) ramené à la forme produite ici, pour
  comparer des fichiers entre eux (body_hash) :
    se:* / sans préfixe → sld:*, SvgParameter → CssParameter ; `horline` tourné → shape://slash | backslash |
    vertline | horline ; equilateral_triangle → triangle, rectangle → square ; hrefs (`D:\\SOG\\…\\X.svg?…`)
    → <préfixe>X.svg?… ; doublons « plain fallback » et <Stroke/> vides supprimés ; Gap → stroke-dasharray ;
    VendorOption QGIS supprimées.
"""
import copy
import hashlib
import re
from xml.sax.saxutils import escape

from lxml import etree

NS_SLD = "http://www.opengis.net/sld"
NS_SE = "http://www.opengis.net/se"
NS_OGC = "http://www.opengis.net/ogc"
NS_XLINK = "http://www.w3.org/1999/xlink"
SYMBOLIZERS = ("PolygonSymbolizer", "LineSymbolizer", "PointSymbolizer", "TextSymbolizer")
QGIS_VENDOR_OPTIONS = {"widthHeightFactor", "placement"}
WKN_RENAME = {"equilateral_triangle": "triangle", "rectangle": "square"}


def local(el):
    return etree.QName(el).localname if isinstance(el.tag, str) else None


def children(el, name):
    return [c for c in el if local(c) == name]


def child(el, name):
    for c in el:
        if local(c) == name:
            return c
    return None


def parse(path_or_text):
    parser = etree.XMLParser(remove_comments=True, remove_blank_text=True)
    if isinstance(path_or_text, (bytes, str)) and str(path_or_text).lstrip().startswith("<"):
        data = path_or_text.encode() if isinstance(path_or_text, str) else path_or_text
        return etree.fromstring(data, parser)
    return etree.parse(str(path_or_text), parser).getroot()


def rules(root):
    return [el for el in root.iter() if local(el) == "Rule"]


def symbolizers(rule):
    return [c for c in rule if local(c) in SYMBOLIZERS]


# --- Réécritures ------------------------------------------------------------------------------

def _rename_ns(el):
    """Tout élément non-ogc passe dans l'espace sld, SvgParameter → CssParameter."""
    for e in el.iter():
        if not isinstance(e.tag, str):
            continue
        q = etree.QName(e)
        if q.namespace == NS_OGC:
            continue
        name = "CssParameter" if q.localname == "SvgParameter" else q.localname
        e.tag = f"{{{NS_SLD}}}{name}"


def _literal_value(el):
    """Texte d'un élément contenant éventuellement un ogc:Literal."""
    if el is None:
        return None
    lit = child(el, "Literal")
    return ((lit.text if lit is not None else el.text) or "").strip()


def _set_literal(el, value):
    for c in list(el):
        el.remove(c)
    el.text = value


def _hatch_from_horline(angle):
    """Angle QGIS (anti-horaire, degrés) d'une ligne → (WellKnownName, rotation SLD horaire ou None)."""
    a = angle % 180
    if abs(a) < 1e-6:
        return "shape://horline", None
    if abs(a - 45) < 1e-6:
        return "shape://slash", None
    if abs(a - 90) < 1e-6:
        return "shape://vertline", None
    if abs(a - 135) < 1e-6:
        return "shape://backslash", None
    return "shape://horline", -a


def _fix_marks(el):
    for graphic in [g for g in el.iter() if local(g) == "Graphic"]:
        mark = child(graphic, "Mark")
        if mark is None:
            continue
        wkn = child(mark, "WellKnownName")
        name = (wkn.text or "").strip() if wkn is not None else ""
        if name.startswith("ttf://"):
            # ttf://Wingdings#0x00f1 → ttf://Wingdings#0xF1 (écriture de la production)
            wkn.text = re.sub(r"#0x0*([0-9a-fA-F]+)$", lambda m: "#0x" + m.group(1).upper(), name)
        elif name.startswith(("shape://", "file://", "extshape://", "wkt://")):
            pass
        elif name in ("horline", "vertline", "slash", "backslash", "times", "dot", "plus"):
            rot_el = child(graphic, "Rotation")
            angle = float(_literal_value(rot_el) or 0)
            if name == "vertline":
                angle += 90
            if name in ("horline", "vertline"):
                new_name, rot = _hatch_from_horline(angle)
                wkn.text = new_name
                # shape://slash|backslash : diagonale de la tuile → espacement = taille / √2
                size_el = child(graphic, "Size")
                if new_name in ("shape://slash", "shape://backslash") and size_el is not None:
                    try:
                        size_el.text = _num(float(_literal_value(size_el)) * 2 ** .5)
                    except ValueError:
                        pass
                if rot is None:
                    if rot_el is not None:
                        graphic.remove(rot_el)
                else:
                    _set_literal(rot_el, _num(rot))
            else:
                wkn.text = "shape://" + name
        elif name in WKN_RENAME:
            wkn.text = WKN_RENAME[name]
        for stroke in children(mark, "Stroke"):
            if len(stroke) == 0:
                mark.remove(stroke)
        # GeoServer ignore toute la marque (contour compris) si son remplissage a fill-opacity 0
        for fill in children(mark, "Fill"):
            op = [p for p in children(fill, "CssParameter") if p.get("name") == "fill-opacity"]
            if op and float(op[0].text or 1) == 0:
                mark.remove(fill)


def _num(v):
    return ("%.2f" % v).rstrip("0").rstrip(".")


def normalize_href(href, prefix):
    """D:\\SOG\\SG5\\PLU\\X.svg?q / svg/cnig/X.svg / http://…/X.svg → prefix + X.svg?q."""
    if not href or not re.search(r"\.(svg|png)", href, re.I):
        return href
    path, _, query = href.partition("?")
    name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return prefix + name + ("?" + query if query else "")


def _fix_graphics(el, href_prefix):
    for graphic in [g for g in el.iter() if local(g) == "Graphic"]:
        seen = set()
        for ext in children(graphic, "ExternalGraphic"):
            res = child(ext, "OnlineResource")
            href = res.get(f"{{{NS_XLINK}}}href") if res is not None else None
            new = normalize_href(href, href_prefix) if href_prefix is not None else href
            base = (new or "").split("?")[0]
            # QGIS écrit SVG paramétrique puis « plain fallback » : on garde le premier
            if base in seen:
                graphic.remove(ext)
                continue
            seen.add(base)
            if res is not None and new:
                res.set(f"{{{NS_XLINK}}}href", new)
                res.set(f"{{{NS_XLINK}}}type", "simple")


def _fix_graphic_stroke_gap(el):
    """<Gap> (SE 1.1, ignoré par GeoServer en SLD 1.0) → stroke-dasharray « taille espace » du Stroke.

    QGIS écrit l'intervalle entre centres de symboles dans Gap ; GeoServer espace un GraphicStroke
    avec un dasharray dont le 1er terme est la taille du symbole et le 2e l'espace entre symboles."""
    for stroke in [s for s in el.iter() if local(s) == "Stroke"]:
        gs = child(stroke, "GraphicStroke")
        if gs is None:
            continue
        gap = child(gs, "Gap")
        if gap is None:
            continue
        gs.remove(gap)
        graphic = child(gs, "Graphic")
        size_el = child(graphic, "Size") if graphic is not None else None
        try:
            interval, size = float(_literal_value(gap)), float(_literal_value(size_el))
        except (TypeError, ValueError):
            continue
        if any(p.get("name") == "stroke-dasharray" for p in children(stroke, "CssParameter")):
            continue
        p = etree.SubElement(stroke, f"{{{NS_SLD}}}CssParameter", name="stroke-dasharray")
        p.text = f"{_num(size)} {_num(max(0.0, interval - size))}"


def _fix_vendor_options(sym):
    """VendorOption QGIS supprimées, les autres placées en fin de symboliseur ; celles mal placées
    (dans Graphic, Mark…) supprimées."""
    for vo in children(sym, "VendorOption"):
        sym.remove(vo)
        if vo.get("name") not in QGIS_VENDOR_OPTIONS:
            sym.append(vo)
    for vo in [v for v in sym.iter() if local(v) == "VendorOption" and v.getparent() is not sym]:
        vo.getparent().remove(vo)


def normalize_symbolizer(sym, href_prefix=None):
    """Copie normalisée d'un symboliseur (élément lxml) ; href_prefix : préfixe des images (None : inchangées)."""
    sym = copy.deepcopy(sym)
    _rename_ns(sym)
    _fix_marks(sym)
    _fix_graphic_stroke_gap(sym)
    _fix_graphics(sym, href_prefix)
    _fix_vendor_options(sym)
    return sym


def body_hash(syms):
    """Empreinte de symboliseurs (pour rapprocher des symboles identiques)."""
    return hashlib.sha1("\n".join(to_xml(s) for s in syms).encode()).hexdigest()


# --- Sérialisation au format production ------------------------------------------------------

SLD_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<sld:StyledLayerDescriptor xmlns:sld="http://www.opengis.net/sld" xmlns="http://www.opengis.net/sld"
  xmlns:ogc="http://www.opengis.net/ogc" xmlns:gml="http://www.opengis.net/gml"
  xmlns:xlink="http://www.w3.org/1999/xlink" version="1.0.0">
  <sld:NamedLayer>
    <sld:Name>{name}</sld:Name>
    <sld:UserStyle>
      <sld:Name>{name}</sld:Name>
      <sld:FeatureTypeStyle>
        <sld:Name>name</sld:Name>
"""
SLD_FOOTER = """      </sld:FeatureTypeStyle>
    </sld:UserStyle>
  </sld:NamedLayer>
</sld:StyledLayerDescriptor>
"""


def filter_xml(conditions, depth):
    """ogc:Filter indenté : conditions = [(attribut, valeur)], égalités combinées en And."""
    pad = "  " * depth

    def equal(prop, value, d):
        p = "  " * d
        return (f"{p}<ogc:PropertyIsEqualTo>\n{p}  <ogc:PropertyName>{prop}</ogc:PropertyName>\n"
                f"{p}  <ogc:Literal>{escape(value)}</ogc:Literal>\n{p}</ogc:PropertyIsEqualTo>")

    if len(conditions) == 1:
        inner = equal(*conditions[0], depth + 1)
    else:
        inner = (f"{pad}  <ogc:And>\n" + "\n".join(equal(*c, depth + 2) for c in conditions)
                 + f"\n{pad}  </ogc:And>")
    return f"{pad}<ogc:Filter>\n{inner}\n{pad}</ogc:Filter>"


def rule_xml(name, title, conditions, syms, max_scale=None, depth=4):
    """sld:Rule au format production ; conditions : [(attribut, valeur)] ; syms : éléments lxml."""
    pad = "  " * depth
    out = [f"{pad}<sld:Rule>", f"{pad}  <sld:Name>{escape(name)}</sld:Name>"]
    if title:
        out.append(f"{pad}  <sld:Title>{escape(title)}</sld:Title>")
    if conditions:
        out.append(filter_xml(conditions, depth + 1))
    if max_scale is not None:
        out.append(f"{pad}  <sld:MaxScaleDenominator>{max_scale}</sld:MaxScaleDenominator>")
    out += [to_xml(s, depth + 1) for s in syms]
    out.append(f"{pad}</sld:Rule>")
    return "\n".join(out)


def sld_document(name, rules):
    """Document SLD complet (une couche, un style) à partir de règles déjà sérialisées."""
    return SLD_HEADER.format(name=escape(name)) + "\n".join(rules) + "\n" + SLD_FOOTER


def _qname(el):
    q = etree.QName(el)
    prefix = "ogc" if q.namespace == NS_OGC else "sld"
    return f"{prefix}:{q.localname}"


def _attrs(el):
    out = []
    for k, v in el.attrib.items():
        q = etree.QName(k)
        name = f"xlink:{q.localname}" if q.namespace == NS_XLINK else q.localname
        out.append(f' {name}="{escape(v, {chr(34): "&quot;"})}"')
    return "".join(out)


def to_xml(el, depth=0, indent="  "):
    """Élément → texte indenté (2 espaces), préfixes sld:/ogc:, comme STYLES/data/du."""
    pad = indent * depth
    tag = _qname(el)
    attrs = _attrs(el)
    kids = [c for c in el if isinstance(c.tag, str)]
    text = (el.text or "").strip()
    if not kids:
        if text:
            return f"{pad}<{tag}{attrs}>{escape(text)}</{tag}>"
        return f"{pad}<{tag}{attrs}/>"
    lines = [f"{pad}<{tag}{attrs}>"]
    lines += [to_xml(c, depth + 1, indent) for c in kids]
    lines.append(f"{pad}</{tag}>")
    return "\n".join(lines)
