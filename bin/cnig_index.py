#!/usr/bin/env python3
"""
1. Index des symboles du dépôt CNIG DDU-SG5-SYMBOLISATION.

Pour chaque code SYMBOLE (PSC-S_01-03_001, ZONE_U_001_pe, AC1|gen_classe|ass_mh…) : domaine, couche GPU,
attributs métier (typepsc/stypepsc, typeinf/stypeinf, typezone, suptype), libellé, classement,
style XML source et empreinte sha1 de son symbole.

Usage :
  bin/cnig_index.py                 écrit data/cnig_index.json (+ archive data/cnig_index.<sha>.json)
  bin/cnig_index.py --since <sha>   + codes ajoutés / modifiés / supprimés depuis <sha> (data/cnig_diff.json)
"""

import argparse
import csv
import functools
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


def read_grille(name):
    """Lignes d'une grille CSV CNIG (cp1252, séparateur ;)."""
    text = (CNIG_GRILLES / name).read_text(encoding="cp1252")
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


def clean(label):
    return " ".join((label or "").replace("\xa0", " ").split())


def entry(domain, code, **fields):
    """Entrée d'index d'un code (fichier du style XML et empreinte : ajoutés par with_style_symbol)."""
    return {"code": code, "domain": domain, **fields}


def index_psc_inf(domain, transparent):
    """Prescriptions et informations : une entrée par code de surface, de ligne et de point des grilles ; les
    variantes « transparent » (_000) vont dans `transparent`."""
    out = []
    for kind, prefix, attr in (("psc", "PSC", "typepsc"), ("inf", "INF", "typeinf")):
        for row in read_grille(f"grille_{domain}_{kind}.csv"):
            # Variantes _000 « transparent » (grilles PLU depuis CNIG 2a50e2c) : symbole invisible, sans règle
            # (converti, il deviendrait une marque sans remplissage, que GeoServer dessine en gris) ; listées à part
            for geom, col in (("S", "symb_surf"), ("L", "symb_lin"), ("P", "symb_pct")):
                if not row.get(col):
                    continue
                (transparent if row.get("classement") == "transparent" else out).append(
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
    plus les codes ZONE / SECTEUR du style XML absents des grilles (ZONE_A_001 PSMV, SECTEUR_xx)."""
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
    for name in sorted(style_xml_symbols(domain)[1]):
        m = re.match(r"(ZONE|SECTEUR)_([A-Za-z0-9]+)_(\d{3})(?:_(ge|pe))?$", name)
        if not m or name in known or m.group(3) == "000":
            continue
        kind, typ, nnn, scale = m.groups()
        layer, attr = ("zone_urba", "typezone") if kind == "ZONE" else ("secteur_cc", "typesect")
        out.append(
            entry(
                domain,
                name,
                layer=layer,
                attrs={attr: typ},
                variant=nnn,
                label=None,
                classement="hors-grille",
                **({"scale": scale} if scale else {}),
            )
        )
    return out


def index_extra_xml(domain, known):
    """Codes PSC / INF du style XML absents des grilles (ex. PSC-L_05-xx PSMV) ; variantes _000 écartées."""
    out = []
    for name in sorted(style_xml_symbols(domain)[1]):
        m = re.match(r"(PSC|INF)-([SLP])_(\d{2})-(\d{2})_(\d{3})$", name)
        if not m or name in known or m.group(5) == "000":
            continue
        prefix, geom, typ, styp, nnn = m.groups()
        attr = "typepsc" if prefix == "PSC" else "typeinf"
        out.append(
            entry(
                domain,
                name,
                layer=GEOM_LAYER[(prefix, geom)],
                attrs={attr: typ, "s" + attr: styp},
                variant=nnn,
                label=None,
                classement="hors-grille",
            )
        )
    return out


def index_sup():
    """SUP : un code par ligne de la grille (empreinte : symbole du style XML, ajouté par with_style_symbol)."""
    out = []
    for row in read_grille("grille_sup.csv"):
        code = row["symb_sup"]
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
            }
        )
    return out


@functools.cache
def style_xml_symbols(domain):
    """Symboles du style XML QGIS d'un domaine (PLU/QGIS/PLU_CNIG.xml…) → (fichier, {nom : <symbol>})."""
    rel = f"{domain.upper()}/QGIS/{domain.upper()}_CNIG.xml"
    path = CNIG_DIR / rel
    if not path.is_file():
        return rel, {}
    return rel, {s.get("name"): s for s in ET.parse(path).getroot().iterfind("symbols/symbol")}


def with_style_symbol(entries):
    """Symbole du style XML de chaque code (source de la conversion) : fichier « xml » et empreinte (sha1 du
    symbole), qui signale un code modifié d'une version CNIG à l'autre."""
    styles = {}
    for e in entries:
        if e["domain"] not in styles:
            styles[e["domain"]] = style_xml_symbols(e["domain"])
        rel, symbols = styles[e["domain"]]
        symbol = symbols.get(e["code"])
        e["xml"] = rel if symbol is not None else None
        e["sha1"] = hashlib.sha1(ET.tostring(symbol)).hexdigest() if symbol is not None else None
    return entries


def index_transparent_xml(domain, known):
    """Symboles _000 du style XML sans ligne dans les grilles (ex. PSC-S_38-03_000 PLU)."""
    out = []
    for name in sorted(style_xml_symbols(domain)[1]):
        m = re.match(r"(PSC|INF)-([SLP])_(\d{2})-(\d{2})_000$", name)
        if m and name not in known:
            prefix, geom, typ, styp = m.groups()
            attr = "typepsc" if prefix == "PSC" else "typeinf"
            out.append(
                entry(
                    domain,
                    name,
                    layer=GEOM_LAYER[(prefix, geom)],
                    attrs={attr: typ, "s" + attr: styp},
                    variant="000",
                    label=None,
                    classement="hors-grille",
                )
            )
    return out


def build_index():
    """Index : entries (codes à styler) et transparent (variantes _000, symbole invisible, pas de règle)."""
    entries, transparent = [], []
    for domain in ("plu", "psmv"):
        part = index_psc_inf(domain, transparent) + index_zone(domain)
        part += index_extra_xml(domain, {e["code"] for e in part})
        entries += part
        transparent += index_transparent_xml(domain, {e["code"] for e in transparent})
    entries += index_sup()
    return {"sha": cnig_sha(), "entries": with_style_symbol(entries), "transparent": with_style_symbol(transparent)}


def diff_index(old, new):
    """Codes ajoutés, supprimés et modifiés (empreinte différente) entre deux index."""

    def by_key(index):
        return {f"{e['domain']}:{e['code']}": e for e in index["entries"] + index.get("transparent", [])}

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
        missing = sum(1 for e in entries if not e["xml"])
        note = f"  ({missing} sans symbole XML)" if missing else ""
        print(f"   {domain:5} {layer:18} {len(entries):4} codes{note}")

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
