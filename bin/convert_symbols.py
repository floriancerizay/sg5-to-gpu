#!/usr/bin/env python3
"""
3. Bibliothèque de symboles GeoServer depuis le dépôt CNIG : un SLD par code, converti des symboles QGIS
(bin/qgis2sld.py), puis complété par pattern_tiles (tuiles de trame, pictos de ligne), wkt_marks (pictos
natifs). Pas d'étiquette : <labelsettings> du style XML est vide, seuls des formats de texte existent (signalés).

Symboles : bibliothèque de styles QGIS du domaine (PLU/QGIS/PLU_CNIG.xml…), un symbole nommé par code ; les QML
ne sont pas lus, ni le dossier SLD (SLD par code, SVG) : les pictos embarqués sont nommés d'après leur code.
Codes absents du XML : symbole du code principal du type ; à défaut, aucun style (signalé). Les catalogues PDF ne
sont pas utilisés ici (seulement pour la planche).

Sorties :
  data/symbols/script/<domaine>/<code>.sld   une règle par code (Name = code, Title = libellé, filtre
                                              symbole = code), hrefs relatifs « X.svg?… »
  data/symbols/script/index.json             clé « domaine:code » → {file, source, notes}
  data/symbols/script/sup_rules.json         règles d'assiette SUP de ref/sup_assiettes.csv (suptype, typeass…)
  data/symbols/issues.json                   problèmes du symbole d'origine par code : [{text, files}]
  data/svg/                                  images (pictos du style XML nommés par code ; tuiles pat_, rot_)
  data/fonts/                                polices des symboles et des étiquettes
"""

import csv
import functools
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sld
from cnig_index import style_xml_symbols
from config import (
    CNIG_DIR,
    CNIG_INDEX,
    FONTS_DIR,
    GEOMETRY_NAME,
    PROD_IMG_DIR,
    ROOT,
    SCRIPT_DIR,
    SUP_ASSIETTES,
    SVG_DIR,
    SYMBOLS_DIR,
    read_json,
    write_json,
)
from pattern_tiles import stroke_graphics, synthesize
from qgis2sld import Converter, ogc_conditions, props, qml_color
from wkt_marks import native_marks

DOMAINS = ("plu", "psmv", "sup")
ABSENT = "absent du style XML CNIG"
DPI = 90.714  # 0,28 mm/px


def key_of(e):
    return f"{e['domain']}:{e['code']}"


# --- Formats de texte du style XML (signalés : le XML n'a aucun réglage d'étiquette) -----------------


@functools.cache
def xml_text_formats(domain):
    """Formats de texte du style XML d'un domaine → {nom (« ZONE_U_001_txt »…) : fichier}."""
    rel = f"{domain.upper()}/QGIS/{domain.upper()}_CNIG.xml"
    if not (CNIG_DIR / rel).is_file():
        return {}
    return {tf.get("name"): rel for tf in ET.parse(CNIG_DIR / rel).getroot().iterfind("textformats/textformat")}


def text_format_name(code):
    """Format de texte XML d'un code : ZONE_U_001_ge → ZONE_U_001_txt."""
    return re.sub(r"_(ge|pe)$", "", code) + "_txt"


# --- Conversion des symboles CNIG (style XML) ------------------------------------------------------

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


def convert(index):
    """Conversion des symboles CNIG → ({clé de code: entrée}, règles d'assiette SUP).

    Source : bibliothèque de styles QGIS du domaine (PLU_CNIG.xml…), un symbole par code ; les QML ne sont pas lus.
    Règles d'assiette SUP (jeu « classiques », filtres par attributs) : table ref/sup_assiettes.csv."""
    shutil.rmtree(SCRIPT_DIR, ignore_errors=True)
    svg_names = {}  # sha1 → nom des pictos embarqués déjà écrits (tous domaines)
    entries = {key_of(e): e for e in index["entries"]}
    result = {}
    stats = {"tuiles": 0, "pictos de ligne": 0, "pictos natifs": 0}

    def symbolizers(converter, symbol, notes, pitches):
        syms = converter.symbolizers(symbol, notes, pitches)
        syms, n_tiles = synthesize(syms, SVG_DIR)
        stats["tuiles"] += n_tiles
        stats["pictos de ligne"] += stroke_graphics(syms, SVG_DIR, symbol.get("type") == "fill", pitches, GEOMETRY_NAME)
        stats["pictos natifs"] += native_marks(syms, SVG_DIR)
        return syms

    def add(key, symbol, converter, source, notes):
        pitches = {}
        syms = symbolizers(converter, symbol, notes, pitches)
        result[key] = {**write_symbol(entries[key], syms, source), "notes": notes}

    for domain in DOMAINS:
        converter = Converter(SVG_DIR, domain, svg_names)
        xml, xml_symbols = style_xml_symbols(domain)
        for name, symbol in xml_symbols.items():
            if f"{domain}:{name}" in entries:  # hors index : variantes « transparent » _000, symboles annexes
                add(f"{domain}:{name}", symbol, converter, xml, [])
    attach_subcodes(entries, result)
    sup_rules = assiette_rules(entries, result)
    print(f"   conversion : {stats}")
    write_json(SCRIPT_DIR / "index.json", result)
    write_json(SCRIPT_DIR / "sup_rules.json", sup_rules)
    return result, sup_rules


def assiette_rules(entries, result):
    """Règles d'assiette SUP filtrées par attributs (jeu « classiques ») : une par ligne de ref/sup_assiettes.csv
    (suptype, attribut, valeur → code SUP dont le symbole XML est repris), écrites dans
    data/symbols/script/sup_rules/ → entrées de sup_rules.json. Ligne sans code (T8) : pas de règle, la production
    est gardée."""
    with open(SUP_ASSIETTES, encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    out = []
    for row in rows:
        key = f"sup:{row['code']}"
        if not row["code"] or key not in result:
            continue
        attrs = {row["attribut"]: row["valeur"]} if row["attribut"] else {}
        label = entries[key].get("label") or row["code"]
        label = f"{label} – {row['valeur']}" if attrs else label
        syms = sld.symbolizers(sld.rules(sld.parse(SCRIPT_DIR / result[key]["file"]))[0])
        path = SCRIPT_DIR / "sup_rules" / f"{len(out):03d}.sld"
        path.parent.mkdir(parents=True, exist_ok=True)
        rule = sld.rule_xml(row["code"], label, ogc_conditions(row["suptype"], attrs), syms)
        path.write_text(sld.sld_document(row["code"], [rule]), encoding="utf-8")
        out.append(
            {
                "label": label,
                "code": row["code"],
                "suptype": row["suptype"],
                "attrs": attrs,
                "notes": result[key].get("notes", []),
                "file": str(path.relative_to(SCRIPT_DIR)),
            }
        )
    return out


def attach_subcodes(entries, result):
    """Codes absents du style XML : symbole du code principal, tt-00 de même géométrie et même variante
    (PSC-L_15-01 → PSC-L_15-00), sinon le premier code du type (PSMV INF-S_19-02 → INF-S_19-01, pas de 19-00) ;
    en SUP, un code de la même servitude, de même terminaison d'assiette si possible."""

    def copy_from(key, ref_key):
        ref = result[ref_key]
        syms = sld.symbolizers(sld.rules(sld.parse(SCRIPT_DIR / ref["file"]))[0])
        result[key] = {
            **write_symbol(entries[key], syms, f"= {ref_key.split(':', 1)[1]}"),
            "notes": ref.get("notes", []),
        }

    # code principal
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
            copy_from(key, ref_key)


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


SVG_NUMBER = re.compile(r"-?\d*\.?\d+(?:[eE][-+]?\d+)?")


def svg_drawing(path):
    """Signature du dessin d'un SVG à l'échelle et à la translation près : tracés absolus ramenés au viewBox
    (hauteur 1, à 0,5 % près), autres attributs tels quels. Deux SVG de même signature rendent le même picto
    à la même Size SLD (hauteur). → (signature, hauteur du viewBox), ou (None, None) si non comparable."""
    root = etree.parse(str(path), etree.XMLParser(remove_comments=True)).getroot()
    vb = [float(v) for v in (root.get("viewBox") or "").replace(",", " ").split()]
    if len(vb) != 4 or vb[2] <= 0 or vb[3] <= 0:
        return None, None
    x0, y0, h = vb[0], vb[1], vb[3]
    parts = [f"{vb[2] / h:.3f}"]
    for el in root.iter():
        tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
        if tag in ("title", "desc", "svg"):
            continue
        parts.append(f"<{tag}>{(el.text or '').strip()}")
        for key, value in sorted(el.attrib.items()):
            if key != "d":
                parts.append(f"{key}={value}")
            elif re.search(r"[a-zHVSQTA]", value):
                return None, None  # commandes relatives ou à une coordonnée : non comparé
            else:
                xy = [float(v) for v in SVG_NUMBER.findall(value)]
                norm = ",".join(f"{(v - (x0 if i % 2 == 0 else y0)) / h:.3f}" for i, v in enumerate(xy))
                parts.append(f"d={SVG_NUMBER.sub('', value).replace(',', ' ').split()}:{norm}")
    return "|".join(parts), h


def share_svgs():
    """Un seul fichier par dessin de picto : copies PLU / PSMV et même dessin à une autre échelle (« V » de PSC-L_15
    et PSC-P_15) → hrefs des SLD par code renvoyés au fichier retenu (nom le plus court), doublons supprimés.
    Un picto à contour (outline-width > 0, en unités du SVG) ne change de fichier qu'à échelle égale.
    → (pictos référencés avant, après)."""
    slds = sorted(SCRIPT_DIR.rglob("*.sld"))
    href = re.compile(r'xlink:href="([^"?]+)(\?[^"]*)?"')
    used = {m.group(1) for f in slds for m in href.finditer(f.read_text(encoding="utf-8"))}
    used = {n for n in used if not n.startswith(("pat_", "rot_")) and (SVG_DIR / n).is_file()}
    groups = {}
    for name in used:
        signature, height = svg_drawing(SVG_DIR / name)
        groups.setdefault(signature or name, []).append((name, height))
    target = {}  # nom → (nom retenu, hauteur du retenu, hauteur d'origine)
    for members in groups.values():
        keep, keep_h = min(members, key=lambda m: (len(m[0]), m[0]))
        for name, height in members:
            if name != keep:
                target[name] = (keep, keep_h, height)

    def replace(m):
        name, query = m.group(1), m.group(2) or ""
        if name not in target:
            return m.group(0)
        keep, keep_h, height = target[name]
        outline = re.search(r"outline-width=([\d.]+)", query)
        if outline and float(outline.group(1)) > 0 and abs(keep_h - height) > 0.01 * height:
            return m.group(0)
        return f'xlink:href="{keep}{query}"'

    for f in slds:
        text = f.read_text(encoding="utf-8")
        new = href.sub(replace, text)
        if new != text:
            f.write_text(new, encoding="utf-8")
    still = {m.group(1) for f in slds for m in href.finditer(f.read_text(encoding="utf-8"))}
    for name in target:
        if name not in still:
            (SVG_DIR / name).unlink()
    return len(used), len(used & still)


def issue(text, *files):
    """Problème : texte « ⚠ » / « ℹ » et fichiers attendus ou en cause [[chemin, rôle]], chemins du dépôt CNIG
    sauf ceux de sg5-to-gpu (data/…), affichés en infobulle sur la planche."""
    return {"text": text, "files": [list(f) for f in files if f]}


def produced(entry):
    """Fichier SLD produit par sg5-to-gpu pour un code."""
    return f"data/symbols/script/{entry['file']}", "SLD produit (sg5-to-gpu)"


def origin_issues(entry, expected, ref_source):
    """Diagnostics sur l'origine du style d'un code absent du style XML (rattaché au code principal, ou aucun style).
    expected : (style XML du domaine, rôle) où le symbole du code est attendu ;
    ref_source : fichier source du code dont le style est repris, ou None."""
    source = entry["source"] if entry else ""
    if entry is None:
        return [issue(f"⚠ aucun style CNIG : {ABSENT}, sans code principal", expected)]
    if source.startswith("= "):
        code_ref = source[2:]
        taken = (ref_source, f"symbole de {code_ref} repris") if ref_source else None
        return [issue(f"⚠ {ABSENT} : style du code principal {code_ref}", expected, taken)]
    return []


def style_issues(e, entry, source_file):
    """Diagnostics sur le style produit : notes de conversion, pictogrammes, formats de texte, marques de police.
    source_file : fichier CNIG (style XML) d'où vient le symbole."""
    source = (source_file, "symbole QGIS converti") if source_file else None
    out = []
    out.extend(issue(note, source, produced(entry)) for note in entry.get("notes", []))
    text = (SCRIPT_DIR / entry["file"]).read_text(encoding="utf-8")
    hrefs = {h.rsplit("/", 1)[-1] for h in re.findall(r'xlink:href="([^"?]+)', text)}
    out.extend(
        issue(
            f"⚠ pictogramme {name} manquant",
            (f"data/svg/{name}", "pictogramme attendu (sg5-to-gpu)"),
            produced(entry),
            source,
        )
        for name in sorted(hrefs)
        if not name.startswith(("pat_", "rot_")) and not (SVG_DIR / name).is_file()
    )
    fmt_name = text_format_name(e["code"])
    if fmt_name in xml_text_formats(e["domain"]):
        out.append(
            issue(
                f"ℹ réglage d'étiquette (champ, placement, échelles) absent de <labelsettings> du XML : seul le format "
                f"de texte {fmt_name} (apparence) existe.",
                (xml_text_formats(e["domain"])[fmt_name], f"<labelsettings> vide : réglage « {e['code']} » attendu"),
                (xml_text_formats(e["domain"])[fmt_name], f"<textformats> : format {fmt_name} (police, taille, halo)"),
            )
        )
    for font, char in sorted(set(re.findall(r"ttf://([^#<]+)#(0x[0-9A-Fa-f]+)", text))):
        out.append(
            issue(
                f"ℹ marque de police {font} {char} : rendu dépendant des polices de GeoServer",
                source,
                ("__Ressources/", f"fichier de la police {font} attendu"),
            )
        )
    return out


def is_transparent(symbol):
    """Symbole QGIS invisible : chaque couche active a une couleur d'opacité nulle ou un style « aucun »."""
    for layer in symbol.iter("layer"):
        if layer.get("enabled") == "0" or layer.findall("symbol"):
            continue  # couche désactivée, ou ligne / remplissage de pictos (sous-couches vérifiées à part)
        q = props(layer)
        for color_key, style_key in (
            ("color", "style"),
            ("line_color", "line_style"),
            ("outline_color", "outline_style"),
        ):
            if q.get(color_key) is None:
                continue
            _, alpha = qml_color(q[color_key])
            if alpha > 0 and q.get(style_key) not in ("no", "NoBrush", "NoPen"):
                return False
    return True


def transparent_issues(index):
    """Variantes « transparent » (_000) : symbole XML absent ou visible (⚠), symbole sans ligne de grille (ℹ)."""
    out = {}
    for e in index.get("transparent", []):
        folder = e["domain"].upper()
        xml = f"{folder}/QGIS/{folder}_CNIG.xml"
        symbol = style_xml_symbols(e["domain"])[1].get(e["code"])
        found = []
        if symbol is None:
            found.append(
                issue("⚠ variante « transparent » de la grille absente du style XML", (xml, "symbole attendu"))
            )
        elif not is_transparent(symbol):
            found.append(
                issue(
                    "⚠ variante « transparent » de la grille, mais symbole visible dans le style XML", (xml, "symbole")
                )
            )
        if e.get("classement") == "hors-grille":
            found.append(
                issue(
                    "ℹ symbole transparent _000 du style XML sans ligne dans la grille",
                    (xml, "symbole"),
                    (f"__outils-agp-python-gdb__/grille_{e['domain']}_{e['code'][:3].lower()}.csv", "ligne attendue"),
                )
            )
        if found:
            out[key_of(e)] = found
    return out


def build_issues(index, result):
    """Problèmes du symbole d'origine, par code : « ⚠ » non reproductible / défaut, « ℹ » information,
    chacun avec les fichiers attendus ou en cause."""
    entries = {key_of(e): e for e in index["entries"]}

    def source_of(key, seen=()):
        """Fichier CNIG (style XML) d'où vient le symbole d'un code, en suivant les codes repris."""
        source = result.get(key, {}).get("source")
        if not source or key in seen:
            return None
        if not source.startswith("= "):
            return source
        return source_of(f"{key.split(':', 1)[0]}:{source[2:]}", (*seen, key))

    issues = {}
    for key, e in entries.items():
        entry = result.get(key)
        folder = e["domain"].upper()
        expected = (f"{folder}/QGIS/{folder}_CNIG.xml", f"symbole « {e['code']} » attendu")
        ref_source = None
        if entry and entry["source"].startswith("= "):
            ref_source = source_of(f"{e['domain']}:{entry['source'][2:]}")
        found = origin_issues(entry, expected, ref_source)
        if entry:
            found += style_issues(e, entry, source_of(key))
        if found:
            issues[key] = list({i["text"]: i for i in found}.values())
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

    result, sup_rules = convert(index)
    direct = sum(1 for v in result.values() if not v["source"].startswith("="))
    print(
        f"✅ {len(result)} symboles ({direct} symboles CNIG, {len(result) - direct} codes rattachés à un autre), "
        f"{len(sup_rules)} règles d'assiette SUP (ref/sup_assiettes.csv) → {SCRIPT_DIR.relative_to(ROOT)}"
    )

    n = svg_dimensions()
    before, after = share_svgs()
    print(f"   pictos SVG : {before} fichiers référencés → {after} après regroupement des dessins identiques")
    print(
        f"   {len(list(SVG_DIR.glob('*.svg')))} images dans {SVG_DIR.relative_to(ROOT)} "
        f"({n} SVG complétés de width/height)"
    )

    issues = {**build_issues(index, result), **transparent_issues(index)}
    write_json(SYMBOLS_DIR / "issues.json", issues)
    warn = sum(1 for v in issues.values() if any(i["text"].startswith("⚠") for i in v))
    print(
        f"✅ diagnostics : {len(issues)} codes signalés dont {warn} avec « ⚠ » → "
        f"{(SYMBOLS_DIR / 'issues.json').relative_to(ROOT)}"
    )


if __name__ == "__main__":
    main()
