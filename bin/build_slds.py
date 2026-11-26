#!/usr/bin/env python3
"""
4. Assemblage des SLD par couche GPU, à partir des SLD par code (data/symbols/script) :

  data/slds/classiques/   organisation de STYLES/SLDS, notsup renommé du (du/<couche>_du|_psmv.sld, sup/<catégorie>/…) :
                          filtres sur les attributs métier (typepsc/stypepsc, typeinf/stypeinf, typezone,
                          typesect, suptype), une seule variante par type / sous-type (la première), repli par
                          type (symbole tt-00) pour les sous-types absents du CNIG, étiquettes des zones sur
                          « libelle », règles de production conservées pour ce que le CNIG ne couvre pas
  data/slds/symboles/     mêmes couches, filtres « symbole = code », toutes les variantes, étiquettes des zones
                          sur « typezone »
  data/slds/img/          pictogrammes et tuiles référencés (URL : SLD_IMG_URL)
  data/slds/fonts/        polices (marques et étiquettes)
  data/slds/DIFFERENCES.md  écarts avec le CNIG et la production (échelles, variantes, règles conservées…)

Échelles : celles de la production (STYLES/SLDS/README.md) — informations / prescriptions < 1:15 000 ; zones
et secteurs : « lowscale » 1:15 000 – 1:150 000 (variante _pe du CNIG) et « highscale » < 1:15 000 (_ge) ;
SUP : échelles de STYLES/data/sup/<suptype>/assiette_sup_s.xml.

Usage : bin/build_slds.py [--validate]   (--validate : publication d'essai sur le GeoServer local)
"""
import argparse
import copy
import csv
import hashlib
import re
import shutil
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (CNIG_INDEX, COUCHES_DIR, FONTS_DIR, GEOSERVER_URL, ROOT, SCRIPT_DIR, SLD_IMG_URL, SLDS_DIR,
                    STYLES_DIR, SVG_DIR, read_json)
import geoserver
import sld_normalize as N
from qml2sld import ogc_conditions

OUT = SLDS_DIR
DOMAIN_SUFFIX = {"plu": "du", "psmv": "psmv"}
# couche GPU → (attribut du type, attribut du sous-type)
KEYS = {"prescription_surf": ("typepsc", "stypepsc"), "prescription_lin": ("typepsc", "stypepsc"),
        "prescription_pct": ("typepsc", "stypepsc"), "info_surf": ("typeinf", "stypeinf"),
        "info_lin": ("typeinf", "stypeinf"), "info_pct": ("typeinf", "stypeinf"),
        "zone_urba": ("typezone", None), "secteur_cc": ("typesect", None)}
DUAL_SCALE = {"zone_urba", "secteur_cc"}  # règles lowscale / highscale
# (MinScaleDenominator, MaxScaleDenominator) des échelles de production
SCALES = {"detail": (None, 15000), "lowscale": (15000, 150000), "highscale": (None, 15000)}
PROD_ONLY = ("document", "municipality", "scot", "mec")  # couches hors CNIG, reprises telles quelles
SUP_TABLES = ("generateur_sup_p", "assiette_sup_p", "generateur_sup_l", "assiette_sup_l", "generateur_sup_s",
              "assiette_sup_s")
# écarts listés dans DIFFERENCES.md, par rubrique
NOTES = {"Échelles": [], "Variantes écartées (classiques)": [], "Règles de production conservées": [],
         "Règles de production abandonnées": [], "Couches reprises de la production": [], "Sans style CNIG": []}


# --- lecture ------------------------------------------------------------------------------------

def load():
    """(entrées de l'index CNIG, SLD par code, règles d'assiette SUP) des étapes 1 et 3."""
    return (read_json(CNIG_INDEX)["entries"], read_json(SCRIPT_DIR / "index.json"),
            read_json(SCRIPT_DIR / "sup_rules.json"))


def symbolizers(entry):
    """Symboliseurs d'un SLD par code (entrée de index.json ou de sup_rules.json)."""
    return N.symbolizers(N.rules(N.parse(SCRIPT_DIR / entry["file"]))[0])


def with_label(syms, prop):
    """Symboliseurs dont les étiquettes portent sur l'attribut prop (libelle / typezone)."""
    out = []
    for s in syms:
        s = copy.deepcopy(s)
        if N.local(s) == "TextSymbolizer":
            for p in s.iter(f"{{{N.NS_OGC}}}PropertyName"):
                p.text = prop
        out.append(s)
    return out


def without_labels(syms):
    return [s for s in syms if N.local(s) != "TextSymbolizer"]


# --- images -------------------------------------------------------------------------------------

USED_IMAGES = set()  # images référencées, copiées dans data/slds/img


def publish_hrefs(syms):
    """hrefs relatifs « X.svg?… » → SLD_IMG_URL/X.svg?…&v=<empreinte> (cache GeoServer par URL)."""
    for s in syms:
        for res in s.iter(f"{{{N.NS_SLD}}}OnlineResource"):
            href = res.get(f"{{{N.NS_XLINK}}}href") or ""
            if "://" in href:
                continue
            name, _, query = href.partition("?")
            path = SVG_DIR / name
            if path.is_file():
                USED_IMAGES.add(name)
                v = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
                query = f"{query}&v={v}" if query else f"v={v}"
            res.set(f"{{{N.NS_XLINK}}}href", f"{SLD_IMG_URL}{name}" + (f"?{query}" if query else ""))
    return syms


# --- écriture de règles -------------------------------------------------------------------------

def eq(prop, value):
    return (f"<ogc:PropertyIsEqualTo><ogc:PropertyName>{prop}</ogc:PropertyName>"
            f"<ogc:Literal>{escape(value)}</ogc:Literal></ogc:PropertyIsEqualTo>")


def rule(name, title, filter_inner, syms, scale, depth=4):
    """sld:Rule ; filter_inner : contenu XML d'ogc:Filter (une seule condition racine)."""
    pad = "  " * depth
    mn, mx = SCALES[scale] if isinstance(scale, str) else scale
    out = [f"{pad}<sld:Rule>", f"{pad}  <sld:Name>{escape(name)}</sld:Name>"]
    if title:
        out.append(f"{pad}  <sld:Title>{escape(title)}</sld:Title>")
    out.append(f"{pad}  <ogc:Filter>{filter_inner}</ogc:Filter>")
    if mn is not None:
        out.append(f"{pad}  <sld:MinScaleDenominator>{mn}</sld:MinScaleDenominator>")
    if mx is not None:
        out.append(f"{pad}  <sld:MaxScaleDenominator>{mx}</sld:MaxScaleDenominator>")
    out += [N.to_xml(s, depth + 1) for s in publish_hrefs(syms)]
    out.append(f"{pad}</sld:Rule>")
    return "\n".join(out)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- règles de production -----------------------------------------------------------------------

def _branches(flt):
    """Branches « ou » d'un filtre de production : [{attribut: valeur}] ; None si filtre complexe
    (différent de, nul…)."""
    root = next(iter(flt), None)
    if root is None:
        return None
    if any(N.local(c) in ("PropertyIsNotEqualTo", "PropertyIsNull", "Not") for c in flt.iter()):
        return None
    tops = list(root) if N.local(root) == "Or" else [root]
    out = []
    for t in tops:
        pairs = [t] if N.local(t) == "PropertyIsEqualTo" else [c for c in t if N.local(c) == "PropertyIsEqualTo"]
        out.append({(N.child(p, "PropertyName").text or "").strip(): (N.child(p, "Literal").text or "").strip()
                    for p in pairs})
    return out


def prod_rules(layer, covered_types, out_name):
    """Règles de production d'une couche non couvertes par le CNIG (type absent des QML) : conservées, filtre
    réduit aux valeurs non couvertes ; règles d'exclusion (« autres ») abandonnées."""
    path = STYLES_DIR / "data/du" / f"{layer}.sld"
    if not path.is_file():
        return []
    type_attr = KEYS[layer][0]
    kept = []
    for r in N.rules(N.parse(path)):
        name = (N.child(r, "Name").text or "").strip()
        flt = N.child(r, "Filter")
        branches = _branches(flt) if flt is not None else None
        if branches is None:
            NOTES["Règles de production abandonnées"].append(f"{out_name} : « {name} » (filtre d'exclusion, couvert "
                                                             f"par les replis CNIG)")
            continue
        free = [b for b in branches if b.get(type_attr) not in covered_types]
        if not free:
            continue
        if len(free) < len(branches):  # filtre réduit aux valeurs non couvertes
            inner = "".join("<ogc:And>" + "".join(eq(k, v) for k, v in b.items()) + "</ogc:And>"
                            if len(b) > 1 else eq(*next(iter(b.items()))) for b in free)
            r.replace(flt, ogc_filter(f"<ogc:Or>{inner}</ogc:Or>" if len(free) > 1 else inner))
            NOTES["Règles de production conservées"].append(
                f"{out_name} : « {name} » réduite à {', '.join(b[type_attr] for b in free)}")
        else:
            NOTES["Règles de production conservées"].append(f"{out_name} : « {name} »")
        kept.append(N.to_xml(r, 4))
    return kept


def ogc_filter(inner):
    """Élément ogc:Filter à partir de son contenu XML."""
    return etree.fromstring(f'<ogc:Filter xmlns:ogc="{N.NS_OGC}">{inner}</ogc:Filter>')


# --- couches DU / PSMV --------------------------------------------------------------------------

def zone_base(code):
    """Code de zone sans suffixe d'échelle : ZONE_U_001_ge → ZONE_U_001."""
    return re.sub(r"_(ge|pe)$", "", code)


def build_du(index, styles):
    """Couches PLU (suffixe _du) et PSMV (_psmv), plus les couches hors CNIG copiées de la production."""
    for domain, suffix in DOMAIN_SUFFIX.items():
        for layer, (tattr, sattr) in KEYS.items():
            out_name = f"{layer}_{suffix}"
            entries = [e for e in index if e["domain"] == domain and e["layer"] == layer]
            styled = [e for e in entries if f"{domain}:{e['code']}" in styles]
            for e in entries:
                if f"{domain}:{e['code']}" not in styles:
                    NOTES["Sans style CNIG"].append(f"{out_name} : {e['code']}")
            if layer == "secteur_cc" and domain == "psmv":  # pas de secteurs PSMV : copie du DU (comme la production)
                styled = [e for e in index if e["domain"] == "plu" and e["layer"] == layer
                          and f"plu:{e['code']}" in styles]
            if layer in DUAL_SCALE:
                classic, symbols = dual_scale_rules(styled, styles, layer, tattr)
            else:
                classic, symbols = detail_rules(styled, styles, tattr, sattr, out_name)
            types = {e["attrs"].get(tattr) for e in styled}
            classic += prod_rules(layer, types, out_name)
            write(OUT / "classiques/du" / f"{out_name}.sld", N.sld_document(layer, classic))
            if symbols:
                write(OUT / "symboles/du" / f"{out_name}.sld", N.sld_document(layer, symbols))
    for name in PROD_ONLY:
        src = STYLES_DIR / "SLDS/notsup" / f"{name}.sld"
        if src.is_file():
            shutil.copyfile(src, OUT / "classiques/du" / src.name)
            NOTES["Couches reprises de la production"].append(f"du/{src.name}")


def detail_rules(styled, styles, tattr, sattr, out_name):
    """Informations / prescriptions : une règle par type-sous-type (classiques), une par code (symboles)."""
    classic, symbols = [], []
    groups = {}
    for e in sorted(styled, key=lambda e: e["code"]):
        groups.setdefault((e["attrs"][tattr], e["attrs"].get(sattr)), []).append(e)
        key = f"{e['domain']}:{e['code']}"
        symbols.append(rule(e["code"], e.get("label"), eq("symbole", e["code"]), symbolizers(styles[key]), "detail"))
    by_type = {}
    for (t, st), es in sorted(groups.items()):
        first = es[0]
        for other in es[1:]:
            NOTES["Variantes écartées (classiques)"].append(f"{out_name} : {other['code']} (gardé : {first['code']})")
        by_type.setdefault(t, []).append(st)
        syms = without_labels(symbolizers(styles[f"{first['domain']}:{first['code']}"]))
        classic.append(rule(f"{t}-{st}", first.get("label"),
                            f"<ogc:And>{eq(tattr, t)}{eq(sattr, st)}</ogc:And>", syms, "detail"))
    for t, sts in sorted(by_type.items()):  # repli : autres sous-types du type, symbole tt-00
        ref = (groups.get((t, "00")) or groups[(t, sts[0])])[0]
        others = "".join(eq(sattr, st) for st in sts)
        flt = (f"<ogc:And>{eq(tattr, t)}<ogc:Not>{'<ogc:Or>' + others + '</ogc:Or>' if len(sts) > 1 else others}"
               f"</ogc:Not></ogc:And>")
        syms = without_labels(symbolizers(styles[f"{ref['domain']}:{ref['code']}"]))
        classic.append(rule(f"{t}-autres", f"{ref.get('label')} (autres sous-types)", flt, syms, "detail"))
    return classic, symbols


def dual_scale_rules(styled, styles, layer, tattr):
    """Zones / secteurs : lowscale (variante _pe) et highscale (_ge) ; étiquettes sur libelle (classiques) ou
    typezone (symboles)."""
    classic, symbols = [], []
    by_type = {}
    for e in styled:
        by_type.setdefault(e["attrs"][tattr], {})[e.get("scale") or ""] = e
    for t, variants in sorted(by_type.items()):
        low = variants.get("pe") or variants.get("") or variants.get("ge")
        high = variants.get("ge") or variants.get("") or variants.get("pe")
        label = [s for s in symbolizers(styles[f"{high['domain']}:{high['code']}"])
                 if N.local(s) == "TextSymbolizer"]
        base = zone_base(high["code"])
        for scale, e in (("lowscale", low), ("highscale", high)):
            syms = without_labels(symbolizers(styles[f"{e['domain']}:{e['code']}"]))
            classic.append(rule(f"{t}-{scale}", e.get("label"), eq(tattr, t), syms + with_label(label, "libelle"), scale))
            symbols.append(rule(f"{base}-{scale}", e.get("label"), eq("symbole", base),
                                syms + with_label(label, tattr), scale))
    if any(v.get("ge") or v.get("pe") for v in by_type.values()):
        NOTES["Échelles"].append(f"{layer} : variantes CNIG _ge (≥ 1:2 500) / _pe (< 1:2 500) placées sur les échelles "
                                 f"de production : _pe en « lowscale » 1:15 000 – 1:150 000, _ge en « highscale » "
                                 f"< 1:15 000")
    return classic, symbols


# --- SUP ----------------------------------------------------------------------------------------

def prod_sup_fragment(suptype, table):
    """Règles de production d'une table SUP (entre <!--BEGIN--> et <!--END--> de STYLES/data/sup), ou None."""
    path = STYLES_DIR / "data/sup" / suptype / f"{table}.xml"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    m = re.search(r"<!--BEGIN-->(.*)<!--END-->", text, re.S)
    return m.group(1).strip() if m else None


def prod_sup_scale(suptype):
    """(min, max) de la 1re règle de production d'assiette surfacique d'un suptype."""
    frag = prod_sup_fragment(suptype, "assiette_sup_s") or ""
    mn = re.search(r"MinScaleDenominator>\s*([\d.]+)", frag)
    mx = re.search(r"MaxScaleDenominator>\s*([\d.]+)", frag)
    return (mn.group(1) if mn else None, mx.group(1) if mx else None)


def build_sup(index, styles, sup_rules):
    """Couches SUP par catégorie (COUCHES/CouchesWMS.csv) et table : règles d'assiette CNIG pour les assiettes
    surfaciques (classiques), une règle par code (symboles), fragments de production pour le reste."""
    categories = read_json(COUCHES_DIR / "supByCategory.json")
    with open(COUCHES_DIR / "CouchesWMS.csv", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f, delimiter=";") if r["domaine"] == "sup"]
    cnig = {}
    for r in sup_rules:
        cnig.setdefault(r["suptype"], []).append(r)
    codes = {}
    for e in index:
        if e["domain"] == "sup":
            codes.setdefault(e["attrs"]["suptype"], []).append(e)
    seen_scale = set()
    for row in rows:
        base = row["nom"]
        suptypes = [s.strip().lower() for s in row["tables_sup_cat"].split(",") if s.strip()]
        for table in SUP_TABLES:
            members = [s for s in suptypes if table in categories.get(s, [])]
            if not members:
                continue
            classic, symbols = [], []
            for st in members:
                scale = prod_sup_scale(st)
                if table == "assiette_sup_s" and st in cnig:
                    for r in cnig[st]:
                        classic.append(rule(r["label"], r["label"], _and(ogc_conditions(st, r["attrs"])),
                                            symbolizers(r), scale))
                        if st not in seen_scale and str(r.get("max_scale") or "") != str(scale[1] or ""):
                            NOTES["Échelles"].append(f"SUP {st} : QML 1:{r.get('max_scale') or '—'} max, production "
                                                     f"{scale[0] or '—'} – {scale[1] or '—'} (production gardée)")
                            seen_scale.add(st)
                else:
                    frag = prod_sup_fragment(st, table)
                    if frag:
                        classic.append(frag)
                        if table == "assiette_sup_s":
                            NOTES["Règles de production conservées"].append(f"sup/{base}/{table} : {st} (pas de règle CNIG)")
                if table == "assiette_sup_s":
                    for e in sorted(codes.get(st, []), key=lambda e: e["code"]):
                        key = f"sup:{e['code']}"
                        if key in styles:
                            symbols.append(rule(e["code"], e.get("label"), eq("symbole", e["code"]),
                                                symbolizers(styles[key]), scale))
            if classic:
                write(OUT / "classiques/sup" / base / f"{table}.sld", N.sld_document("One style", classic))
            if symbols:
                write(OUT / "symboles/sup" / base / f"{table}.sld", N.sld_document("One style", symbols))


def _and(conditions):
    parts = [eq(p, v) for p, v in conditions]
    return parts[0] if len(parts) == 1 else "<ogc:And>" + "".join(parts) + "</ogc:And>"


# --- images, polices, différences, validation ---------------------------------------------------

def copy_resources():
    """Images référencées et polices, à déployer avec les SLD."""
    (OUT / "img").mkdir(parents=True, exist_ok=True)
    for name in sorted(USED_IMAGES):
        shutil.copyfile(SVG_DIR / name, OUT / "img" / name)
    (OUT / "fonts").mkdir(parents=True, exist_ok=True)
    for f in FONTS_DIR.glob("*"):
        shutil.copyfile(f, OUT / "fonts" / f.name)


def write_differences():
    lines = ["# Différences avec le CNIG et la production", "",
             "Généré par `bin/build_slds.py`. Images : `" + SLD_IMG_URL + "` (dossier `img/` à y déployer), "
             "polices : dossier `fonts/` (à installer dans `styles/fonts` de GeoServer).", ""]
    for title, items in NOTES.items():
        lines += [f"## {title} ({len(items)})", ""]
        lines += [f"- {i}" for i in items] or ["- aucune"]
        lines.append("")
    write(OUT / "DIFFERENCES.md", "\n".join(lines))


def validate():
    """Publication d'essai de chaque SLD sur le GeoServer local (workspace temporaire) ; refus listés."""
    ws = "gpu_slds_check"
    code, _ = geoserver.recreate_workspace(ws)
    if not geoserver.ok(code):
        print(f"   ⚠ validation impossible : GeoServer injoignable ({GEOSERVER_URL})")
        return
    errors = 0
    files = sorted(OUT.rglob("*.sld"))
    for i, f in enumerate(files):
        code, body = geoserver.add_style(ws, f"s{i}", f.read_bytes())
        if not geoserver.ok(code):
            errors += 1
            print(f"   ⚠ {f.relative_to(OUT)} : {code} {body[:200]!r}")
    geoserver.delete_workspace(ws)
    print(f"✅ validation GeoServer : {len(files) - errors}/{len(files)} SLD acceptés")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--validate", action="store_true", help="publication d'essai sur le GeoServer local")
    args = ap.parse_args()
    shutil.rmtree(OUT, ignore_errors=True)
    index, styles, sup_rules = load()
    build_du(index, styles)
    build_sup(index, styles, sup_rules)
    for name in ("assiette_sup_l", "assiette_sup_p"):  # assiettes non surfaciques : pas de style CNIG
        NOTES["Couches reprises de la production"].append(f"sup/*/{name}.sld (fragments STYLES/data/sup)")
    copy_resources()
    write_differences()
    n = {d: len(list((OUT / d).rglob("*.sld"))) for d in ("classiques", "symboles")}
    print(f"✅ {OUT.relative_to(ROOT)} : {n['classiques']} SLD classiques, {n['symboles']} SLD par symbole, "
          f"{len(USED_IMAGES)} images ; écarts → DIFFERENCES.md")
    if args.validate:
        validate()


if __name__ == "__main__":
    main()
