#!/usr/bin/env python3
"""
1. Index des symboles du dépôt CNIG DDU-SG5-SYMBOLISATION.

Pour chaque code SYMBOLE (PSC-S_01-03_001, ZONE_U_001_pe, AC1|gen_classe|ass_mh…) : domaine, couche GPU,
attributs métier (typepsc/stypepsc, typeinf/stypeinf, typezone, suptype), libellé, classement,
fichiers sources (SLD par code, SVG, QML) et empreinte sha1.

Usage :
  bin/cnig_index.py                 écrit data/cnig_index.json (+ archive data/cnig_index.<sha>.json)
  bin/cnig_index.py --since <sha>   + codes ajoutés / modifiés / supprimés depuis <sha> (data/cnig_diff.json)
"""

import argparse
import csv
import hashlib
import io
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CNIG_DIR, CNIG_GRILLES, CNIG_INDEX, ROOT, cnig_sha, read_json, write_json

# (famille, géométrie) → couche GPU
GEOM_LAYER = {
    ("PSC", "S"): "prescription_surf",
    ("PSC", "L"): "prescription_lin",
    ("PSC", "P"): "prescription_pct",
    ("INF", "S"): "info_surf",
    ("INF", "L"): "info_lin",
    ("INF", "P"): "info_pct",
}
SVG_HREF_RE = re.compile(r'xlink:href="([^"?]+\.svg)', re.IGNORECASE)


def read_grille(name):
    """Lignes d'une grille CSV CNIG (cp1252, séparateur ;)."""
    text = (CNIG_GRILLES / name).read_text(encoding="cp1252")
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


def clean(label):
    return " ".join((label or "").replace("\xa0", " ").split())


def sld_files(domain, code):
    """(fichier SLD, [noms des SVG référencés]) d'un code dans <DOMAINE>/SLD ; (None, []) s'il n'existe pas."""
    sld = CNIG_DIR / domain.upper() / "SLD" / f"{code}.sld"
    if not sld.is_file():
        return None, []
    svgs = []
    for href in SVG_HREF_RE.findall(sld.read_text(encoding="utf-8")):
        name = href.replace("\\", "/").rsplit("/", 1)[-1]
        if name not in svgs:
            svgs.append(name)
    return sld, svgs


def fingerprint(paths):
    h = hashlib.sha1()
    for p in paths:
        h.update(p.name.encode())
        h.update(p.read_bytes() if p.is_file() else b"<absent>")
    return h.hexdigest()


def entry(domain, code, **fields):
    """Entrée d'index d'un code PLU / PSMV, avec son SLD par code et ses SVG."""
    sld, svgs = sld_files(domain, code)
    files = [sld] + [sld.parent / s for s in svgs] if sld else []
    return {
        "code": code,
        "domain": domain,
        **fields,
        "sld": str(sld.relative_to(CNIG_DIR)) if sld else None,
        "svg": svgs,
        "sha1": fingerprint(files) if files else None,
    }


def index_psc_inf(domain):
    """Prescriptions et informations : une entrée par code de surface, de ligne et de point des grilles."""
    out = []
    for kind, prefix, attr in (("psc", "PSC", "typepsc"), ("inf", "INF", "typeinf")):
        for row in read_grille(f"grille_{domain}_{kind}.csv"):
            # Variantes _000 « transparent » (grilles PLU depuis CNIG 2a50e2c) : symbole invisible, sans règle
            # (converti, il deviendrait une marque sans remplissage, que GeoServer dessine en gris)
            if row.get("classement") == "transparent":
                continue
            for geom, col in (("S", "symb_surf"), ("L", "symb_lin"), ("P", "symb_pct")):
                if not row.get(col):
                    continue
                out.append(
                    entry(
                        domain,
                        row[col],
                        layer=GEOM_LAYER[(prefix, geom)],
                        attrs={attr: row[attr], "s" + attr: row["s" + attr]},
                        variant=row["nnn"],
                        label=clean(row["lib_stype"]),
                        classement=row["classement"],
                        # Grilles PLU depuis CNIG 2a50e2c : `etiquette` vide pour les sous-codes, l'étiquette
                        # complète est dans `etq_total` (même valeur que l'ancienne colonne `etiquette`)
                        etiquette=row.get("etiquette") or row.get("etq_total"),
                    )
                )
    return out


def index_zone(domain):
    """Zonage : PLU en deux variantes d'échelle (_ge ≥ 1:2500, _pe < 1:2500), PSMV en une ;
    plus les codes ZONE / SECTEUR présents en SLD mais absents des grilles (ZONE_A_001 PSMV, SECTEUR_xx)."""
    out = []
    for row in read_grille(f"grille_{domain}_zonage.csv"):
        common = {
            "layer": "zone_urba",
            "attrs": {"typezone": row["typezone"]},
            "variant": "001",
            "label": clean(row["lib_type"]),
            "classement": "symbole",
        }
        if domain == "plu":
            for col, scale in (("symb_sup2500", "ge"), ("symb_inf2500", "pe")):
                out.append(entry(domain, row[col], **common, scale=scale))
        else:
            out.append(entry(domain, row["symbole"], **common))

    known = {e["code"] for e in out}
    for sld in sorted((CNIG_DIR / domain.upper() / "SLD").glob("*.sld")):
        m = re.match(r"(ZONE|SECTEUR)_([A-Za-z0-9]+)_(\d{3})(?:_(ge|pe))?$", sld.stem)
        if not m or sld.stem in known:
            continue
        kind, typ, nnn, scale = m.groups()
        layer, attr = ("zone_urba", "typezone") if kind == "ZONE" else ("secteur_cc", "typesect")
        out.append(
            entry(
                domain,
                sld.stem,
                layer=layer,
                attrs={attr: typ},
                variant=nnn,
                label=None,
                classement="hors-grille",
                **({"scale": scale} if scale else {}),
            )
        )
    return out


def index_extra_sld(domain, known):
    """SLD par code PSC / INF absents des grilles (ex. PSC-L_05-xx PSMV)."""
    out = []
    for sld in sorted((CNIG_DIR / domain.upper() / "SLD").glob("*.sld")):
        m = re.match(r"(PSC|INF)-([SLP])_(\d{2})-(\d{2})_(\d{3})$", sld.stem)
        if not m or sld.stem in known:
            continue
        prefix, geom, typ, styp, nnn = m.groups()
        attr = "typepsc" if prefix == "PSC" else "typeinf"
        out.append(
            entry(
                domain,
                sld.stem,
                layer=GEOM_LAYER[(prefix, geom)],
                attrs={attr: typ, "s" + attr: styp},
                variant=nnn,
                label=None,
                classement="hors-grille",
            )
        )
    return out


def qml_symbols(qml_path):
    """Symboles d'un QML CNIG → {valeur de catégorie ou libellé de règle : (<symbol>, infos de la règle)}."""
    renderer = ET.parse(qml_path).getroot().find("renderer-v2")
    symbols = {s.get("name"): s for s in renderer.find("symbols")}
    out = {}
    if renderer.get("type") == "categorizedSymbol":
        for cat in renderer.find("categories"):
            out[cat.get("value")] = (symbols.get(cat.get("symbol")), {})
    elif renderer.get("type") == "RuleRenderer":
        for rule in renderer.find("rules").iter("rule"):
            symbol = symbols.get(rule.get("symbol"))
            if symbol is None:
                continue
            label, n = rule.get("label"), 2
            while label in out:  # libellé en double (ex. I3 dans ASSIETTE_SUP_S) : règles distinctes
                label, n = f"{rule.get('label')} ({n})", n + 1
            out[label] = (
                symbol,
                {
                    "filter": rule.get("filter"),
                    "scalemaxdenom": rule.get("scalemaxdenom"),
                    "scalemindenom": rule.get("scalemindenom"),
                },
            )
    return out


def index_sup():
    """SUP : un code par ligne de la grille ; empreinte = celle du symbole de la catégorie du QML."""
    qml = CNIG_DIR / "SUP/QGIS/QML/sup_ass.qml"
    symbols = qml_symbols(qml)
    out = []
    for row in read_grille("grille_sup.csv"):
        code = row["symb_sup"]
        symbol = symbols.get(code, (None, {}))[0]
        out.append(
            {
                "code": code,
                "domain": "sup",
                "layer": "assiette_sup_s",
                "attrs": {"suptype": row["typesup"].lower()},
                "variant": None,
                "label": clean(row["lib_sup"]),
                "classement": row["classement"],
                "etiquette": row.get("etiquette"),
                "qml": str(qml.relative_to(CNIG_DIR)),
                "sha1": hashlib.sha1(ET.tostring(symbol)).hexdigest() if symbol is not None else None,
            }
        )
    return out


def build_index():
    entries = []
    for domain in ("plu", "psmv"):
        part = index_psc_inf(domain) + index_zone(domain)
        part += index_extra_sld(domain, {e["code"] for e in part})
        entries += part
    entries += index_sup()
    return {"sha": cnig_sha(), "entries": entries}


def diff_index(old, new):
    """Codes ajoutés, supprimés et modifiés (empreinte différente) entre deux index."""

    def by_key(index):
        return {f"{e['domain']}:{e['code']}": e for e in index["entries"]}

    o, n = by_key(old), by_key(new)
    return {
        "added": sorted(set(n) - set(o)),
        "removed": sorted(set(o) - set(n)),
        "changed": sorted(k for k in set(o) & set(n) if o[k]["sha1"] != n[k]["sha1"]),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="SHA d'un index archivé (data/cnig_index.<sha>.json) à comparer")
    args = ap.parse_args()

    index = build_index()
    CNIG_INDEX.parent.mkdir(parents=True, exist_ok=True)
    write_json(CNIG_INDEX, index)
    if index["sha"]:
        write_json(CNIG_INDEX.with_name(f"cnig_index.{index['sha'][:7]}.json"), index)

    by_layer = {}
    for e in index["entries"]:
        by_layer.setdefault((e["domain"], e["layer"]), []).append(e)
    print(
        f"✅ {CNIG_INDEX.relative_to(ROOT)} — CNIG {index['sha'] and index['sha'][:7]}, {len(index['entries'])} codes"
    )
    for (domain, layer), entries in sorted(by_layer.items()):
        missing = sum(1 for e in entries if domain != "sup" and not e["sld"])
        print(f"   {domain:5} {layer:18} {len(entries):4} codes" + (f"  ({missing} sans SLD)" if missing else ""))

    if args.since:
        old = read_json(CNIG_INDEX.with_name(f"cnig_index.{args.since[:7]}.json"))
        diff = diff_index(old, index)
        diff_path = CNIG_INDEX.with_name("cnig_diff.json")
        write_json(diff_path, diff)
        print(
            f"Depuis {args.since[:7]} : +{len(diff['added'])} / ~{len(diff['changed'])} / -{len(diff['removed'])}"
            f" → {diff_path.relative_to(ROOT)}"
        )


if __name__ == "__main__":
    main()
