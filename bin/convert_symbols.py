#!/usr/bin/env python3
"""
3. Bibliothèque de symboles GeoServer depuis le dépôt CNIG : un SLD par code, converti des QML CNIG
(bin/qml2sld.py), puis complété par pattern_tiles (tuiles de trame, pictos de ligne) et par les étiquettes
des couches ArcGIS (.lyrx).

Codes absents des QML (sous-codes) : symbole du code QML dont le fichier SLD CNIG est identique, sinon celui
du code principal du type ; à défaut, aucun style (signalé). Les fichiers SLD par code du dépôt CNIG ne servent
qu'à cette comparaison. Les catalogues PDF ne sont pas utilisés ici (seulement pour la planche).

Sorties :
  data/symbols/script/<domaine>/<code>.sld   une règle par code (Name = code, Title = libellé, filtre
                                              symbole = code), hrefs relatifs « X.svg?… »
  data/symbols/script/index.json             clé « domaine:code » → {file, source, notes}
  data/symbols/script/sup_rules.json         règles d'ASSIETTE_SUP_S.qml (suptype, typeass, échelle)
  data/symbols/issues.json                   problèmes du symbole d'origine par code (« ⚠ » / « ℹ »)
  data/svg/                                  images (SVG CNIG, PSMV préfixées psmv_ ; tuiles pat_, rot_)
  data/fonts/                                polices des symboles et des étiquettes
"""

import copy
import functools
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from fontTools.ttLib import TTFont
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sld
from cnig_index import qml_symbols
from config import (
    CNIG_DIR,
    CNIG_INDEX,
    FONTS_DIR,
    GEOMETRY_NAME,
    PROD_IMG_DIR,
    ROOT,
    SCRIPT_DIR,
    SVG_DIR,
    SYMBOLS_DIR,
    read_json,
    write_json,
)
from pattern_tiles import stroke_graphics, synthesize
from qml2sld import Converter, ogc_conditions, props, qml_color, rule_filter

QML_DOMAINS = {"PLU": "plu", "PSMV": "psmv", "SUP": "sup"}
LYRX_DOMAINS = {"PLU": "plu", "PSMV": "psmv"}  # seules couches ArcGIS qui définissent des étiquettes
# substituts libres des polices d'étiquette absentes (même dessin ou mêmes métriques)
FONT_SUBSTITUTES = {"Century Gothic": "URW Gothic", "Times New Roman": "Liberation Serif", "Arial": "Liberation Sans"}
DPI = 90.714  # 0,28 mm/px


def key_of(e):
    return f"{e['domain']}:{e['code']}"


# --- Étiquettes des couches ArcGIS (.lyrx) -----------------------------------------------------


def _cim_color(sym):
    """Couleur #rrggbb de la 1re couche d'un symbole CIM ArcGIS, ou None."""
    layers = (sym or {}).get("symbolLayers") or [{}]
    color = layers[0].get("color") or {}
    values = color.get("values") or []
    if color.get("type") != "CIMRGBColor" or len(values) < 3:
        return None
    r, g, b = (int(v) for v in values[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def _sub_element(parent, tag, text=None, **attrs):
    el = etree.SubElement(parent, f"{{{sld.NS_SLD}}}{tag}", **attrs)
    if text is not None:
        el.text = text
    return el


def text_symbolizer(text_symbol, attr):
    """TextSymbolizer d'une classe d'étiquettes ArcGIS (CIMTextSymbol) dont le texte est l'attribut `attr`."""
    sym = etree.Element(f"{{{sld.NS_SLD}}}TextSymbolizer")
    etree.SubElement(_sub_element(sym, "Label"), f"{{{sld.NS_OGC}}}PropertyName").text = attr

    font = _sub_element(sym, "Font")
    _sub_element(font, "CssParameter", text_symbol.get("fontFamilyName") or "Arial", name="font-family")
    _sub_element(font, "CssParameter", f"{float(text_symbol.get('height') or 10) * DPI / 72:.1f}", name="font-size")
    style = (text_symbol.get("fontStyleName") or "").lower()
    if "bold" in style:
        _sub_element(font, "CssParameter", "bold", name="font-weight")
    if "italic" in style:
        _sub_element(font, "CssParameter", "italic", name="font-style")

    anchor = _sub_element(_sub_element(_sub_element(sym, "LabelPlacement"), "PointPlacement"), "AnchorPoint")
    _sub_element(anchor, "AnchorPointX", "0.5")
    _sub_element(anchor, "AnchorPointY", "0.5")

    halo, fill = _cim_color(text_symbol.get("haloSymbol")), _cim_color(text_symbol.get("symbol"))
    if halo and text_symbol.get("haloSize"):
        h = _sub_element(sym, "Halo")
        _sub_element(h, "Radius", f"{float(text_symbol['haloSize']) * DPI / 72:.1f}")
        _sub_element(_sub_element(h, "Fill"), "CssParameter", halo, name="fill")
    if fill:
        _sub_element(_sub_element(sym, "Fill"), "CssParameter", fill, name="fill")
    _sub_element(sym, "VendorOption", "false", name="conflictResolution")
    _sub_element(sym, "VendorOption", "true", name="partials")
    return sym


def lyrx_labels(index):
    """Étiquettes des couches ArcGIS (.lyrx CNIG, seule source qui en définit) → (labels, skipped).

    labels : {clé de code : TextSymbolizer} pour les classes visibles dont la clause where vise un code
    (« symb_lin = 'PSC-L_39-50_001' ») ou un attribut métier (« typzone = 'U' » → codes de la couche dont
    l'attribut vaut 'U') et dont le texte est un champ ($feature.typzone → attribut GPU typezone).
    skipped : {clé : texte} des étiquettes à texte constant (« "12" »), absentes du QML et de la vignette
    du catalogue, non reprises."""
    by_domain = {}
    for e in index["entries"]:
        by_domain.setdefault(e["domain"], []).append(e)
    labels, skipped = {}, {}
    for folder, domain in LYRX_DOMAINS.items():
        for lyrx in sorted((CNIG_DIR / folder / "ArcGISpro/LYRX").glob("*.lyrx")):
            data = json.loads(lyrx.read_text(encoding="utf-8-sig"))
            scale = "_ge" if lyrx.stem.endswith("_ge") else "_pe" if lyrx.stem.endswith("_pe") else ""
            for layer in data.get("layerDefinitions", []):
                if not layer.get("labelVisibility"):
                    continue
                for label_class in layer.get("labelClasses", []):
                    where = re.match(r"^\s*(\w+)\s*=\s*'([^']*)'\s*$", label_class.get("whereClause") or "")
                    if not where or label_class.get("visibility") is False:
                        continue
                    value = where.group(2)
                    entries = by_domain.get(domain, [])
                    targets = [e for e in entries if e["code"] == value] or [
                        e
                        for e in entries
                        if value in e["attrs"].values()
                        and e["code"].startswith("ZONE_") == ("zone" in lyrx.stem)
                        and e["code"].endswith(scale)
                    ]
                    expr = (label_class.get("expression") or "").strip()
                    field = re.match(r"^\$feature\.(\w+)$", expr)
                    constant = re.match(r'^"([^"]*)"$', expr)
                    if not targets or not (field or constant):
                        continue
                    if constant:
                        skipped.update({key_of(e): constant.group(1) for e in targets})
                        continue
                    text_symbol = label_class.get("textSymbol", {}).get("symbol", {})
                    for e in targets:
                        # attribut GPU : celui de l'entrée qui porte la valeur filtrée (typzone → typezone)
                        attr = next((k for k, v in e["attrs"].items() if v == value), field.group(1).lower())
                        labels[key_of(e)] = text_symbolizer(text_symbol, attr)
    return labels, skipped


@functools.cache
def _font_files(family):
    """Fichiers d'une famille de polices : polices de production (gpu-entrepot STYLES/data/fonts),
    sinon polices système (fontconfig)."""
    found = []
    for f in sorted(PROD_IMG_DIR.glob("*")):
        try:
            if TTFont(str(f), lazy=True)["name"].getBestFamilyName() == family:
                found.append(f)
        except Exception:
            continue
    if found:
        return found
    r = subprocess.run(["fc-list", family, "family", "style", "file"], capture_output=True, text=True)
    paths = (line.split(":")[0] for line in r.stdout.splitlines())
    return sorted(Path(p) for p in paths if p.lower().endswith((".ttf", ".otf")))


def resolve_font(family):
    """Police d'étiquette utilisable par GeoServer → (famille retenue, message ou None) ; fichiers copiés dans
    data/fonts (installés sur GeoServer par compare.py)."""
    for candidate, substitute in ((family, False), (FONT_SUBSTITUTES.get(family), True)):
        files = _font_files(candidate) if candidate else []
        if not files:
            continue
        FONTS_DIR.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copyfile(f, FONTS_DIR / f.name)
        if substitute:
            return candidate, f"⚠ police d'étiquette {family} absente de __Ressources"
        return candidate, None
    return family, f"⚠ police d'étiquette {family} absente : GeoServer utilisera une police de substitution"


def zone_color(symbol):
    """Couleur de la zone : 1er trait non gris du symbole QML (liseré coloré des zones _ge)."""
    for layer in symbol.findall("layer"):
        if layer.get("class") == "SimpleLine":
            color, _ = qml_color(props(layer).get("line_color"))
            if color and len({color[1:3], color[3:5], color[5:7]}) > 1:
                return color
    return None


def fit_label(label, symbol, key, notes):
    """Étiquette d'un code : police résolue ; halo d'une zone aligné sur la couleur de la zone quand le .lyrx
    diverge (AUc : halo noir dans le .lyrx, rouge dans le catalogue comme pour les autres zones)."""
    font = label.find(f".//{{{sld.NS_SLD}}}CssParameter[@name='font-family']")
    if font is not None:
        font.text, note = resolve_font(font.text)
        if note:
            notes.append(note)
    halo = label.find(f"{{{sld.NS_SLD}}}Halo/{{{sld.NS_SLD}}}Fill/{{{sld.NS_SLD}}}CssParameter")
    color = zone_color(symbol) if key.split(":")[1].startswith("ZONE_") else None
    if halo is not None and color and halo.text.lower() != color:
        notes.append(
            f"⚠ halo de l'étiquette du .lyrx ({halo.text}) différent de la couleur de la zone ({color}, "
            f"comme dans le catalogue) : couleur de la zone retenue"
        )
        halo.text = color
    return label


# --- Conversion des QML ------------------------------------------------------------------------

SUBCODE_RE = re.compile(r"^(.+_\d{2})-(\d{2})(_\d{3}.*)$")  # PSC-P_15-01_001 : type 15, sous-type 01, variante 001


def base_code(code):
    """Code de base d'un sous-code : PSC-P_15-01_001 → PSC-P_15-00_001 (None si déjà un code de base)."""
    m = SUBCODE_RE.match(code)
    return f"{m.group(1)}-00{m.group(3)}" if m and m.group(2) != "00" else None


def same_type(code):
    """Clé (préfixe et type, variante) d'un code PSC / INF : PSC-P_15-01_001 → ("PSC-P_15", "_001")."""
    m = SUBCODE_RE.match(code)
    return (m.group(1), m.group(3)) if m else None


def write_symbol(entry, syms, source):
    """SLD d'un code → entrée de data/symbols/script/index.json."""
    path = SCRIPT_DIR / entry["domain"] / f"{entry['code'].replace('|', '__')}.sld"
    path.parent.mkdir(parents=True, exist_ok=True)
    rule = sld.rule_xml(entry["code"], entry.get("label"), [("symbole", entry["code"])], syms)
    path.write_text(sld.sld_document(entry["code"], [rule]), encoding="utf-8")
    return {"file": str(path.relative_to(SCRIPT_DIR)), "source": source}


def cnig_svg_by_sha1():
    """sha1 du contenu → {domaine: nom} des SVG CNIG, pour nommer les pictos embarqués dans les QML."""
    out = {}
    for folder in ("PLU", "PSMV"):
        for f in sorted((CNIG_DIR / folder / "SLD").glob("*.svg")):
            out.setdefault(hashlib.sha1(f.read_bytes()).hexdigest(), {}).setdefault(folder.lower(), f.name)
    return out


def convert(index, labels):
    """Conversion de tous les QML CNIG → ({clé de code: entrée}, règles d'assiette SUP)."""
    shutil.rmtree(SCRIPT_DIR, ignore_errors=True)
    svg_names = cnig_svg_by_sha1()
    entries = {key_of(e): e for e in index["entries"]}
    result, sup_rules, stats = {}, [], {"tuiles": 0, "pictos de ligne": 0}
    for folder, domain in QML_DOMAINS.items():
        converter = Converter(SVG_DIR, domain, svg_names)
        for qml in sorted((CNIG_DIR / folder / "QGIS/QML").glob("*.qml")):
            for name, (symbol, info) in qml_symbols(qml).items():
                if symbol is None:
                    continue
                is_rule = info.get("filter") is not None  # règle d'ASSIETTE_SUP_S, sinon catégorie = code
                if not is_rule and (name or "").endswith("_000"):
                    continue  # variante « transparent » : écartée de l'index (cnig_index.py), pas de symbole
                notes, pitches = [], {}
                syms = converter.symbolizers(symbol, notes, pitches)
                syms, n_tiles = synthesize(syms, SVG_DIR)
                stats["tuiles"] += n_tiles
                stats["pictos de ligne"] += stroke_graphics(
                    syms, SVG_DIR, symbol.get("type") == "fill", pitches, GEOMETRY_NAME
                )
                if is_rule:
                    rule = sup_rule(name, info, syms, notes, len(sup_rules))
                    if rule:
                        sup_rules.append(rule)
                    continue
                if name in ("", "NULL", None):  # catégorie « toutes les autres valeurs »
                    continue
                key = f"{domain}:{name}"
                if key in labels:
                    syms.append(fit_label(copy.deepcopy(labels[key]), symbol, key, notes))
                if key not in entries:
                    print(f"  ⚠ code QML inconnu de l'index {key} ({qml.name})")
                    continue
                result[key] = {**write_symbol(entries[key], syms, f"{folder}/QGIS/QML/{qml.name}"), "notes": notes}
    attach_subcodes(entries, result)
    print(f"   conversion : {stats}")
    write_json(SCRIPT_DIR / "index.json", result)
    write_json(SCRIPT_DIR / "sup_rules.json", sup_rules)
    return result, sup_rules


def sup_rule(name, info, syms, notes, number):
    """Règle d'ASSIETTE_SUP_S écrite dans data/symbols/script/sup_rules/ → entrée de sup_rules.json, ou None."""
    parsed = rule_filter(info["filter"])
    if parsed is None:
        print(f"  ⚠ filtre de règle non traduit : {name} ({info['filter']})")
        return None
    suptype, attrs = parsed
    path = SCRIPT_DIR / "sup_rules" / f"{number:03d}.sld"
    path.parent.mkdir(parents=True, exist_ok=True)
    rule = sld.rule_xml(name, name, ogc_conditions(suptype, attrs), syms, max_scale=info.get("scalemaxdenom"))
    path.write_text(sld.sld_document(name, [rule]), encoding="utf-8")
    return {
        "label": name,
        "suptype": suptype,
        "attrs": attrs,
        "notes": notes,
        "max_scale": info.get("scalemaxdenom"),
        "file": str(path.relative_to(SCRIPT_DIR)),
    }


def sld_signature(path):
    """Empreinte des symboliseurs d'un fichier SLD CNIG (comparaison de sous-codes uniquement)."""
    root = sld.parse(path)
    return sld.body_hash(
        [sld.normalize_symbolizer(s, href_prefix="") for r in sld.rules(root) for s in sld.symbolizers(r)]
    )


def attach_subcodes(entries, result):
    """Codes absents des QML, dans l'ordre :
    1. symbole du code QML dont le fichier SLD CNIG est identique ;
    2. symbole du code principal : tt-00 de même géométrie et même variante (PSC-L_15-01 → PSC-L_15-00), sinon
       le premier code QML du type (PSMV INF-S_19-02 → INF-S_19-01, pas de 19-00) ; en SUP, un code de la même
       servitude, de même terminaison d'assiette si possible."""

    def copy_from(key, ref_key, **flags):
        ref = result[ref_key]
        syms = sld.symbolizers(sld.rules(sld.parse(SCRIPT_DIR / ref["file"]))[0])
        result[key] = {
            **write_symbol(entries[key], syms, f"= {ref_key.split(':', 1)[1]}"),
            "notes": ref.get("notes", []),
            **flags,
        }

    # 1. fichier SLD CNIG identique
    signature = {k: sld_signature(CNIG_DIR / e["sld"]) for k, e in entries.items() if e.get("sld")}
    by_signature = {}
    for k in sorted(result):
        if k in signature:
            by_signature.setdefault((entries[k]["domain"], signature[k]), k)
    for key, e in entries.items():
        if key not in result and (e["domain"], signature.get(key)) in by_signature:
            copy_from(key, by_signature[(e["domain"], signature[key])])

    # 2. code principal
    direct = sorted(k for k in result if not result[k]["source"].startswith("="))
    for key, e in entries.items():
        if key in result:
            continue
        if e["domain"] == "sup":
            head, _, tail = e["code"].partition("|")
            same = [k for k in direct if k.startswith(f"sup:{head}|")]
            ref_key = next(
                (k for k in same if k.rsplit("|", 1)[-1] == tail.rsplit("|", 1)[-1]), same[0] if same else None
            )
        else:
            base = f"{e['domain']}:{base_code(e['code'])}"
            kind = same_type(e["code"])
            first = next(
                (
                    k
                    for k in direct
                    if kind and k.startswith(f"{e['domain']}:") and same_type(k.split(":", 1)[1]) == kind
                ),
                None,
            )
            ref_key = base if base in result else first
        if ref_key:
            copy_from(key, ref_key, main_code=True)


# --- Images, polices et diagnostics ------------------------------------------------------------


def svg_dimensions():
    """width / height (repris du viewBox) ajoutés aux SVG qui n'en ont pas : GeoServer n'affiche pas
    certains d'entre eux (ex. trait 8 × 2 de PSC-L_25-00_001). → nombre de fichiers modifiés."""
    n = 0
    for f in SVG_DIR.glob("*.svg"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"<svg\b[^>]*>", text)
        if not m or re.search(r"\swidth\s*=", m.group(0)):
            continue
        vb = re.search(r'viewBox\s*=\s*"([^"]+)"', m.group(0))
        parts = vb.group(1).replace(",", " ").split() if vb else []
        if len(parts) != 4:
            continue
        tag = f'<svg width="{parts[2]}" height="{parts[3]}"' + m.group(0)[4:]
        f.write_text(text[: m.start()] + tag + text[m.end() :], encoding="utf-8")
        n += 1
    return n


def origin_issues(e, entry):
    """Diagnostics sur l'origine du style d'un code (QML, sous-code rattaché, aucun style)."""
    out = []
    source = entry["source"] if entry else ""
    ref = source[2:]
    if entry is None and e.get("sld"):
        out.append(
            f"⚠ absent des QML CNIG, sans code QML équivalent ni code principal : aucun style produit "
            f"(son fichier {e['sld']} n'est pas utilisé)"
        )
    elif entry is None:
        out.append("⚠ aucun style CNIG (ni QML, ni fichier SLD par code)")
    elif source.startswith("= ") and entry.get("main_code"):
        if e.get("sld"):
            out.append(f"⚠ absent des QML CNIG ; pourtant, le SLD de {Path(e['sld']).name} diffère de celui de {ref}")
        else:
            out.append(f"⚠ absent des QML CNIG : style du code principal {ref}")
    elif source.startswith("= "):
        out.append(f"ℹ absent des QML CNIG : style de {ref} (fichier SLD identique)")
    return out


def style_issues(key, entry, labels, skipped):
    """Diagnostics sur le style produit : pictogrammes, étiquettes, marques de police."""
    out = list(entry.get("notes", []))
    text = (SCRIPT_DIR / entry["file"]).read_text(encoding="utf-8")
    for name in sorted({h.rsplit("/", 1)[-1] for h in re.findall(r'xlink:href="([^"?]+)', text)}):
        if name.startswith(("pat_", "rot_")):
            continue
        if not (SVG_DIR / name).is_file():
            out.append(f"⚠ pictogramme {name} manquant")
            continue
        if name.startswith("qml_"):
            out.append(f"ℹ pictogramme du QML absent des SVG CNIG ({name})")
    if key in labels:
        out.append("ℹ étiquette reprise de la couche ArcGIS (.lyrx), absente du QML et du SLD CNIG")
    if key in skipped:
        out.append(
            f"ℹ étiquette « {skipped[key]} » du .lyrx non reprise (texte constant, absent du QML "
            f"et de la vignette du catalogue)"
        )
    for font, char in sorted(set(re.findall(r"ttf://([^#<]+)#(0x[0-9A-Fa-f]+)", text))):
        out.append(f"ℹ marque de police {font} {char} : rendu dépendant des polices de GeoServer")
    return out


def build_issues(index, result, labels, skipped):
    """Problèmes du symbole d'origine, par code : « ⚠ » non reproductible / défaut, « ℹ » information."""
    issues = {}
    for e in index["entries"]:
        key = key_of(e)
        entry = result.get(key)
        found = origin_issues(e, entry)
        if entry:
            found += style_issues(key, entry, labels, skipped)
        if found:
            issues[key] = list(dict.fromkeys(found))
    return issues


def symbol_fonts():
    """Polices des symboles dans data/fonts, avant la conversion : pattern_tiles en tire les tracés des marques de
    police (trames Wingdings…), build_slds les livre avec les SLD, compare.py les installe sur GeoServer.
    Sources : STYLES/data/fonts de gpu-entrepot et la police Mostly du dépôt CNIG."""
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    for f in PROD_IMG_DIR.glob("*.[tT][tT][fF]"):
        shutil.copyfile(f, FONTS_DIR / f.name)
    with zipfile.ZipFile(CNIG_DIR / "__Ressources/240829 mostly_waves.zip") as z:
        for name in z.namelist():
            if name.lower().endswith(".ttf"):
                (FONTS_DIR / Path(name).name).write_bytes(z.read(name))


def main():
    index = read_json(CNIG_INDEX)
    symbol_fonts()
    shutil.rmtree(SVG_DIR, ignore_errors=True)  # régénéré entièrement à chaque passe
    SVG_DIR.mkdir(parents=True)

    labels, skipped = lyrx_labels(index)
    print(f"   étiquettes ArcGIS (lyrx) : {len(labels)} codes")
    result, sup_rules = convert(index, labels)
    direct = sum(1 for v in result.values() if not v["source"].startswith("="))
    print(
        f"✅ {len(result)} symboles ({direct} QML, {len(result) - direct} sous-codes rattachés), "
        f"{len(sup_rules)} règles ASSIETTE_SUP_S → {SCRIPT_DIR.relative_to(ROOT)}"
    )

    n = svg_dimensions()
    print(
        f"   {len(list(SVG_DIR.glob('*.svg')))} images dans {SVG_DIR.relative_to(ROOT)} "
        f"({n} SVG complétés de width/height)"
    )

    issues = build_issues(index, result, labels, skipped)
    write_json(SYMBOLS_DIR / "issues.json", issues)
    warn = sum(1 for v in issues.values() if any(i.startswith("⚠") for i in v))
    print(
        f"✅ diagnostics : {len(issues)} codes signalés dont {warn} avec « ⚠ » → "
        f"{(SYMBOLS_DIR / 'issues.json').relative_to(ROOT)}"
    )


if __name__ == "__main__":
    main()
