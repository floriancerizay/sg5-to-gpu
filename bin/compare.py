#!/usr/bin/env python3
"""
5. Planche de comparaison des symboles CNIG, colonnes
  attendu (catalogue PDF CNIG) | QGIS (style CNIG) | Script | problèmes du symbole d'origine | prod actuelle

1. Grille de test data/layer/compare.gpkg : une couche par (domaine, couche GPU), géométries des vignettes du
   catalogue (TEMPLATES, extraites des PDF vectoriels). Prescriptions et informations : une cellule par fiche
   « tt-ss_nnn » où surface, ligne et point reprennent le gabarit de la fiche ; zones : la croix des fiches
   de zonage ; SUP : le polygone des fiches SUP.
   Attributs : symbole + attributs métier (typepsc/stypepsc, typeinf/stypeinf, typezone, suptype, typeass…).
2. Styles publiés sur le GeoServer local (workspace GEOSERVER_CMP_WORKSPACE) :
   prod = STYLES/data/du/<couche>.sld (ou SLDS/sup/sup/assiette_sup_s.sld) appliqué aux attributs métier,
   script = SLD par code de l'étape 3 (règles « symbole = code »).
3. Une image GetMap par (couche, style) + une image QGIS (bin/render_qgis.py), superposées par fiche.
4. Écart au rendu QGIS par code → data/symbols/scores.json (signalé au-delà de DEVIATION).
5. report/compare.html ; problèmes : data/symbols/issues.json (étape 3) + écart au rendu QGIS.

Prérequis : étapes 1 à 3, GeoServer local démarré, PyQGIS (sauf --skip-qgis).
Usage : bin/compare.py [--sections plu_prescription,...] [--skip-qgis]
"""

import argparse
import csv
import functools
import html
import re
import shutil
import subprocess
import sys
from pathlib import Path
from string import Template

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import geoserver
import sld
from config import (
    CNIG_DIR,
    CNIG_INDEX,
    FONTS_DIR,
    GEOMETRY_NAME,
    GEOSERVER_CMP_IMG,
    GEOSERVER_CMP_WORKSPACE,
    GEOSERVER_FONTS,
    GPKG,
    PROD_IMG_DIR,
    QGIS_PYTHON,
    REPORT_DIR,
    ROOT,
    SCRIPT_DIR,
    STYLES_DIR,
    SVG_DIR,
    SYMBOLS_DIR,
    read_json,
    write_json,
)

SCORES = SYMBOLS_DIR / "scores.json"
ISSUES = SYMBOLS_DIR / "issues.json"

CELL, STEP, COLS, RES = 60.0, 80.0, 10, 0.5  # m, m, colonnes, m/px  (≈ 1:1800)
X0, Y0 = 700000.0, 6600000.0
TILE = int(STEP / RES)
FIELDS = [
    "id",
    "code",
    "label",
    "classement",
    "symbole",
    "typepsc",
    "stypepsc",
    "typeinf",
    "stypeinf",
    "typezone",
    "typesect",
    "suptype",
    "typeass",
    "type",
    "idass",
    "txt",
    "libelle",
]
VARIANTS = ("qgis", "script", "prod")  # rendus calculés
MAIN_VARIANTS = ("qgis", "script")  # colonnes avant les problèmes ; « prod actuelle » tout à droite
# écart au rendu QGIS au-delà duquel le symbole est signalé (le déphasage des trames est neutralisé par distance)
DEVIATION = 6.0
MAX_SHIFT = 16  # px : décalage de phase des trames toléré par la métrique
OUTLINE_BAND = 6  # px : bande autour du contour d'un polygone, comparée sans décalage
PEAKS = 6  # maxima de corrélation essayés
# Titres des sections de la planche
SECTION_TITLES = {
    "plu_prescription": "PLU · prescriptions",
    "plu_info": "PLU · informations",
    "plu_zone_urba": "PLU · zonage",
    "plu_secteur_cc": "PLU · secteurs de carte communale",
    "psmv_prescription": "PSMV · prescriptions",
    "psmv_info": "PSMV · informations",
    "psmv_zone_urba": "PSMV · zonage",
    "sup": "SUP",
}
GEOM_LETTER = {"S": "surf.", "L": "lin.", "P": "pct."}
# Géométries des vignettes du catalogue CNIG (tracés vectoriels des PDF, repère du cadre ramené à [-1, 1],
# y vers le haut) : fiche prescription / information (PLU p. 70), fiche de zonage (PLU p. 5)
TEMPLATES = {
    "sheet_S": [
        (-0.3997, -0.7774),
        (-0.5784, -0.7555),
        (-0.7555, -0.5329),
        (-0.7774, 0.1771),
        (-0.6223, 0.2225),
        (-0.5784, 0.2006),
        (-0.489, 0.0674),
        (-0.3778, 0.022),
        (-0.2884, 0.0),
        (-0.2445, 0.022),
        (-0.2007, 0.0674),
        (-0.1771, 0.1113),
        (-0.2225, 0.2225),
        (-0.2884, 0.3103),
        (-0.3103, 0.4436),
        (-0.2664, 0.489),
        (-0.1771, 0.4671),
        (0.0439, 0.4436),
        (0.2445, 0.3778),
        (0.3558, 0.2445),
        (0.3997, 0.1113),
        (0.3777, 0.0),
        (0.4216, -0.2226),
        (0.489, -0.2884),
        (0.5329, -0.511),
        (0.4671, -0.5784),
        (0.2665, -0.6222),
        (0.1552, -0.6003),
        (-0.1332, -0.511),
        (-0.2007, -0.5549),
        (-0.2445, -0.6881),
        (-0.3339, -0.7555),
    ],
    "sheet_L": [(0.5549, 0.3339), (0.7774, -0.1113), (0.7335, -0.7774)],
    "sheet_P": [(0.5643, 0.6583)],
    "zone_S": [
        (-0.7774, -0.511),
        (-0.7774, 0.0439),
        (-0.511, 0.0439),
        (-0.511, 0.2884),
        (0.0, 0.2884),
        (0.0, 0.5548),
        (0.7774, 0.5548),
        (0.7774, -0.2226),
        (0.2665, -0.2226),
        (0.2665, -0.7774),
        (-0.2664, -0.7774),
        (-0.2664, -0.511),
    ],
}


def _fit(points, margin=0.92):
    """Polygone recentré et agrandi pour occuper le cadre (fiches SUP : même polygone, plein cadre)."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    cx, cy, half = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(max(xs) - min(xs), max(ys) - min(ys)) / 2
    return [((x - cx) / half * margin, (y - cy) / half * margin) for x, y in points]


TEMPLATES["sup_S"] = _fit(TEMPLATES["sheet_S"])


def geom_of(layer):
    """Géométrie d'une couche de la grille : S (surface), L (ligne) ou P (point)."""
    return "L" if layer.endswith("_lin") else "P" if layer.endswith("_pct") else "S"


# --- 1. Grille de test --------------------------------------------------------------------------


def cell_origin(i):
    """Coin haut-gauche de la cellule i de la grille (COLS colonnes, pas de STEP m)."""
    col, row = i % COLS, i // COLS
    pad = (STEP - CELL) / 2
    return X0 + col * STEP + pad, Y0 - row * STEP - pad


def wkt(geom, i, shape):
    """Géométrie de la cellule i d'après le gabarit du catalogue ; shape : sheet (fiche prescription /
    information), zone ou sup."""
    x, y = cell_origin(i)
    key = f"{shape}_{geom}" if f"{shape}_{geom}" in TEMPLATES else f"sheet_{geom}"
    pts = [(x + CELL / 2 * (1 + u), y - CELL / 2 * (1 - v)) for u, v in TEMPLATES[key]]
    text = ", ".join(f"{px:.3f} {py:.3f}" for px, py in pts)
    if geom == "P":
        return f"POINT({text})"
    if geom == "L":
        return f"LINESTRING({text})"
    return f"POLYGON(({text}, {pts[0][0]:.3f} {pts[0][1]:.3f}))"


def shape_of(name):
    """Gabarit de vignette d'une couche : zone (croix des fiches de zonage), sup ou sheet (fiche PSC / INF)."""
    return "zone" if "zone_urba" in name or "secteur" in name else "sup" if name.startswith("sup_") else "sheet"


def bbox(n):
    """(emprise, taille en px) d'une grille de n cellules."""
    rows = max(1, -(-n // COLS))
    return [X0, Y0 - rows * STEP, X0 + COLS * STEP, Y0], [int(COLS * STEP / RES), int(rows * STEP / RES)]


def build_sections():
    """(couches, sections). couches : nom → lignes ; sections : nom → {layers, groups, sheet, n}."""
    index = read_json(CNIG_INDEX)
    layers, sections = {}, {}

    def add(section, layer, key, row, sheet):
        sec = sections.setdefault(section, {"layers": [], "groups": {}, "sheet": sheet})
        if layer not in sec["layers"]:
            sec["layers"].append(layer)
        key = key if sheet else f"{len(sec['groups']):04d}"
        sec["groups"].setdefault(key, {})[layer] = row
        layers.setdefault(layer, []).append(row)

    for e in index["entries"]:
        row = {
            "code": e["code"],
            "label": e.get("label") or "",
            "classement": e.get("classement") or "",
            "symbole": e["code"],
            "domain": e["domain"],
            **e["attrs"],
        }
        if e["domain"] == "sup":
            row["idass"] = f"{e['attrs']['suptype']}-test"
            add("sup", "sup_symbole", None, row, False)
        elif e["layer"].startswith(("prescription", "info")):
            fam = e["layer"].split("_")[0]
            a = e["attrs"]
            key = f"{a.get('typepsc') or a.get('typeinf')}-{a.get('stypepsc') or a.get('stypeinf')}_{e['variant']}"
            add(f"{e['domain']}_{fam}", f"{e['domain']}_{e['layer']}", key, row, True)
        else:
            add(f"{e['domain']}_{e['layer']}", f"{e['domain']}_{e['layer']}", None, row, False)

    for sec in sections.values():
        keys = sorted(sec["groups"])
        sec["groups"] = [{"key": k, "cell": i, "codes": sec["groups"][k]} for i, k in enumerate(keys)]
        sec["n"] = len(keys)
        for g in sec["groups"]:
            for row in g["codes"].values():
                row["cell"], row["sheet"] = g["cell"], sec["sheet"]
    return layers, sections


def write_gpkg(layers):
    """Grille de test : une couche GeoPackage par couche de la planche (via CSV + ogr2ogr)."""
    GPKG.parent.mkdir(parents=True, exist_ok=True)
    GPKG.unlink(missing_ok=True)
    tmp = GPKG.parent / "compare_csv"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    for name, rows in layers.items():
        path = tmp / f"{name}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([*FIELDS, "WKT"])
            for i, r in enumerate(rows):
                w.writerow(
                    [i + 1] + [r.get(k, "") for k in FIELDS[1:]] + [wkt(geom_of(name), r["cell"], shape_of(name))]
                )
        subprocess.run(
            [
                "ogr2ogr",
                "-f",
                "GPKG",
                GPKG,
                path,
                "-oo",
                "GEOM_POSSIBLE_NAMES=WKT",
                "-oo",
                "AUTODETECT_TYPE=NO",
                "-a_srs",
                "EPSG:2154",
                "-nln",
                name,
                "-lco",
                f"GEOMETRY_NAME={GEOMETRY_NAME}",
            ]
            + (["-update"] if GPKG.exists() else []),
            check=True,
        )
    shutil.rmtree(tmp)


# --- 2. Styles ----------------------------------------------------------------------------------


def geoserver_href(href, sub="", src=None):
    """href local GeoServer + empreinte du contenu (v=…) : GeoServer garde les images en cache par URL,
    un pictogramme modifié sous le même nom resterait affiché dans son ancienne version."""
    url = sld.normalize_href(href, f"file://{GEOSERVER_CMP_IMG}/{sub}")
    path = (src or SVG_DIR) / url.split("?")[0].rsplit("/", 1)[-1]
    if path.is_file():
        url += ("&" if "?" in url else "?") + "v=" + sld.image_version(path)
    return url


def prod_style(name):
    """SLD de production d'une couche de la planche, pictogrammes servis depuis GEOSERVER_CMP_IMG/prod ; ou None."""
    domain, _, layer = name.partition("_")
    # SUP : style des assiettes surfaciques, appliqué à l'attribut suptype de la grille de test
    path = STYLES_DIR / ("SLDS/sup/sup/assiette_sup_s.sld" if domain == "sup" else f"data/du/{layer}.sld")
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    # pictogrammes de production dans un sous-dossier : 24 portent le nom d'un SVG CNIG au dessin différent
    return re.sub(
        r'xlink:href="([^"]+)"',
        lambda m: f'xlink:href="{html.escape(geoserver_href(html.unescape(m.group(1)), "prod/", PROD_IMG_DIR))}"',
        text,
    )


def script_style(name, rows):
    """SLD « symbole = code » d'une couche, depuis data/symbols/script."""
    domain = name.split("_")[0]
    index = read_json(SCRIPT_DIR / "index.json")
    rules = [
        sld.rule_xml(row["code"], row["label"], [("symbole", row["code"])], _symbolizers(index[key]))
        for row in rows
        if (key := f"{domain}:{row['code']}") in index
    ]
    return sld.sld_document(f"script_{name}", rules) if rules else None


def _symbolizers(entry):
    """Symboliseurs d'un SLD par code, images servies depuis GEOSERVER_CMP_IMG."""
    syms = sld.symbolizers(sld.rules(sld.parse(SCRIPT_DIR / entry["file"]))[0])
    for s in syms:
        for res in s.iter(f"{{{sld.NS_SLD}}}OnlineResource"):
            res.set(f"{{{sld.NS_XLINK}}}href", geoserver_href(res.get(f"{{{sld.NS_XLINK}}}href")))
    return syms


# --- GeoServer ----------------------------------------------------------------------------------


def install_resources():
    """SVG → GEOSERVER_CMP_IMG, polices → styles/fonts (redémarrage GeoServer si nouvelle police)."""
    img = Path(GEOSERVER_CMP_IMG)
    img.mkdir(parents=True, exist_ok=True)
    for f in SVG_DIR.glob("*"):
        shutil.copyfile(f, img / f.name)
    (img / "prod").mkdir(exist_ok=True)
    for f in list(PROD_IMG_DIR.glob("*.svg")) + list(PROD_IMG_DIR.glob("*.png")):
        shutil.copyfile(f, img / "prod" / f.name)
    # data/fonts : rempli par bin/convert_symbols.py (polices des symboles et des étiquettes)
    fonts = Path(GEOSERVER_FONTS)
    fonts.mkdir(parents=True, exist_ok=True)
    new = [f.name for f in FONTS_DIR.glob("*") if not (fonts / f.name).exists()]
    for f in FONTS_DIR.glob("*"):
        shutil.copyfile(f, fonts / f.name)
    if new:
        print(f"  ⚠ nouvelles polices copiées dans {fonts} : {new} — redémarrer GeoServer pour les charger")


def publish(styles):
    """Workspace de comparaison recréé : grille de test et styles."""
    ws = GEOSERVER_CMP_WORKSPACE
    code, body = geoserver.recreate_workspace(ws)
    assert geoserver.ok(code), body
    code, body = geoserver.add_geopackage(ws, "compare", GPKG)
    assert geoserver.ok(code), body
    for name, text in styles.items():
        code, body = geoserver.add_style(ws, name, text)
        if not geoserver.ok(code):
            print(f"  ⚠ style {name} : {code} {body[:300]!r}")


def getmap(layer, style, box, size, out):
    """Rendu GeoServer d'une couche de la planche dans le fichier `out`."""
    data, error = geoserver.getmap(GEOSERVER_CMP_WORKSPACE, layer, style, box, size)
    if error is not None:
        print(f"  ⚠ GetMap {layer}/{style} : {error!r}")
        return
    out.write_bytes(data)


# --- 3. Superposition par fiche, 4. choix du moteur ---------------------------------------------


def composite(paths, out):
    """Superpose des rendus sur fond blanc (pixel le plus sombre)."""
    imgs = [Image.open(p).convert("RGB") for p in paths if p.is_file()]
    if not imgs:
        return False
    acc = imgs[0]
    for im in imgs[1:]:
        acc = ImageChops.darker(acc, im)
    acc.save(out)
    return True


def _rgb(path):
    return np.asarray(Image.open(path).convert("RGB").filter(ImageFilter.GaussianBlur(2)), dtype=float)


def _shift_candidates(img, ref, plain, mask=None):
    """Décalages (dy, dx) à essayer pour recaler la phase des trames : principaux maxima de la corrélation
    croisée (≤ MAX_SHIFT px), complétés d'une grille grossière si l'écart sans décalage est notable."""
    g, r = img.mean(axis=2), ref.mean(axis=2)
    if mask is not None:
        g, r = g * mask, r * mask
    g, r = g - g.mean(), r - r.mean()
    xc = np.fft.fftshift(np.fft.irfft2(np.fft.rfft2(r) * np.conj(np.fft.rfft2(g)), s=r.shape))
    h, w = r.shape
    c = MAX_SHIFT
    win = xc[h // 2 - c : h // 2 + c + 1, w // 2 - c : w // 2 + c + 1]
    # maxima locaux de la corrélation : le plus fort est souvent le contour, pas la trame
    peaks = (win == ndimage.maximum_filter(win, size=5)) & (win > 0)
    ranked = sorted(zip(*np.nonzero(peaks), strict=True), key=lambda p: -win[p])[:PEAKS]
    candidates = [(int(py) - c, int(px) - c) for py, px in ranked]
    if plain > 3:
        candidates += [(dy, dx) for dy in range(-8, 9, 2) for dx in range(-8, 9, 2)]
    return [shift for shift in candidates if shift != (0, 0)]


def distance(img, ref, masks=None):
    """Écart moyen (0-255, RVB) entre deux tuiles, après recalage de la phase des trames.

    QGIS cale les motifs de remplissage sur l'emprise de l'objet, GeoServer sur l'origine de l'image : on essaie
    les décalages de _shift_candidates et on garde l'écart minimal (sans décalage compris).
    masks : (intérieur, bande du contour) du polygone de test. L'intérieur est alors recalé ; l'extérieur, calé sur
    la géométrie dans les deux rendus, est comparé sans décalage ; dans la bande, où se touchent contour et trame,
    chaque pixel garde le meilleur des deux. Sans masque (lignes, points), toute la tuile est recalée.
    """
    error = np.abs(img - ref).mean(axis=2)
    plain = float(error.mean())
    c = MAX_SHIFT
    if masks is None:
        best = plain
        for dy, dx in _shift_candidates(img, ref, plain):
            shifted = np.roll(img, (dy, dx), axis=(0, 1))
            best = min(best, float(np.abs(shifted - ref)[c:-c, c:-c].mean()))  # bords enroulés exclus
        return best
    interior, band = masks
    outside = float(error[~(interior | band)].sum())
    best = float(error[interior | band].sum())
    for dy, dx in _shift_candidates(img, ref, plain, interior):
        shifted_error = np.abs(np.roll(img, (dy, dx), axis=(0, 1)) - ref).mean(axis=2)
        total = shifted_error[interior].sum() + np.minimum(error, shifted_error)[band].sum()
        best = min(best, float(total))
    return (outside + best) / error.size


@functools.cache
def polygon_masks(name):
    """(intérieur, bande du contour) du polygone de test des cellules d'une couche surfacique (le même dans chaque
    cellule) : la bande fait OUTLINE_BAND px de part et d'autre du contour. None pour des lignes ou des points."""
    if geom_of(name) != "S":
        return None
    template = f"{shape_of(name)}_S" if f"{shape_of(name)}_S" in TEMPLATES else "sheet_S"
    pad = (STEP - CELL) / 2
    points = [((pad + CELL / 2 * (1 + u)) / RES, (pad + CELL / 2 * (1 - v)) / RES) for u, v in TEMPLATES[template]]
    inside, outline = Image.new("1", (TILE, TILE), 0), Image.new("1", (TILE, TILE), 0)
    ImageDraw.Draw(inside).polygon(points, fill=1)
    ImageDraw.Draw(outline).line([*points, points[0]], fill=1, width=2 * OUTLINE_BAND + 1, joint="curve")
    band = np.asarray(outline, dtype=bool)
    return np.asarray(inside, dtype=bool) & ~band, band


def compute_scores(layers, img_dir):
    """Écart au rendu QGIS par code (codes à symbole propre dans le style XML ; cellule QGIS non vide)."""
    idx = read_json(SCRIPT_DIR / "index.json")
    scores = {}
    for name, rows in layers.items():
        paths = {v: img_dir / f"{v}_{name}.png" for v in ("qgis", "script")}
        if not all(p.is_file() for p in paths.values()):
            continue
        qgis, script = _rgb(paths["qgis"]), _rgb(paths["script"])
        for r in rows:
            key = f"{r['domain']}:{r['code']}"
            col, row = r["cell"] % COLS, r["cell"] // COLS
            sl = (slice(row * TILE, (row + 1) * TILE), slice(col * TILE, (col + 1) * TILE))
            if key in idx and not idx[key]["source"].startswith("=") and qgis[sl].min() < 250:
                scores[key] = round(distance(script[sl], qgis[sl], polygon_masks(name)), 2)
    write_json(SCORES, scores)
    return scores


# --- 5. Planche HTML ----------------------------------------------------------------------------

PAGE = Template((Path(__file__).parent / "compare.html").read_text(encoding="utf-8"))  # gabarit de la planche


def tile(img, i, css="render", badge=""):
    """Cellule du tableau : portion i de l'image d'une section (ou « — »), pastille éventuelle."""
    if img is None:
        return f'<td class="{css}"><div class="na">—</div></td>'
    col, row = i % COLS, i // COLS
    return (
        f'<td class="{css}"><div class="t" style="background-image:url(img/{img});'
        f'background-position:-{col * TILE}px -{row * TILE}px">{badge}</div></td>'
    )


SVG_KINDS = (  # préfixe → (singulier, pluriel)
    ("pat_", ("tuile de trame", "tuiles de trame")),
    ("rot_", ("picto tourné", "pictos tournés")),
    ("", ("picto", "pictos")),
)


@functools.cache
def svg_images(path):
    """Images (SVG) référencées par un SLD par code → noms de fichier, dans l'ordre, sans doublon."""
    names = [
        res.get(f"{{{sld.NS_XLINK}}}href", "").split("?")[0].rsplit("/", 1)[-1]
        for res in sld.parse(path).iter(f"{{{sld.NS_SLD}}}OnlineResource")
    ]
    return tuple(dict.fromkeys(n for n in names if n))


def fiche_svgs(keys, script):
    """Images (SVG) chargées par les styles des codes d'une fiche, dans l'ordre, sans doublon."""
    return list(dict.fromkeys(n for k in keys if k in script for n in svg_images(SCRIPT_DIR / script[k]["file"])))


def svg_kind(name, plural=False):
    return next(labels for prefix, labels in SVG_KINDS if name.startswith(prefix))[plural]


def svg_badge(names):
    """Pastille « SVG » de la vignette script quand le style charge des images, détail en infobulle."""
    if not names:
        return ""
    tip = "\n".join(f"{svg_kind(n)} : {n}" for n in names)
    count = f" ×{len(names)}" if len(names) > 1 else ""
    return f'<span class="svg" title="{html.escape(tip)}">SVG{count}</span>'


def svg_summary(names):
    """« N SVG différents (x tuiles de trame, y pictos…) »."""
    kinds = {}
    for n in sorted(names):
        kinds.setdefault(svg_kind(n), []).append(n)
    detail = ", ".join(f"{len(v)} {svg_kind(v[0], len(v) > 1)}" for v in kinds.values())
    return f"{len(names)} SVG différent{'s' if len(names) > 1 else ''}" + (f" ({detail})" if detail else "")


# Types de problème proposés par le filtre de la planche : (clé, libellé, motif reconnu dans le message).
# Un message prend le premier type dont le motif apparaît ; l'ordre compte (« aucun style » avant « absent »).
PROBLEM_TYPES = [
    ("no-style", "aucun style", "aucun style"),
    ("absent", "code absent du style XML CNIG, style d'un autre code", "absent du style XML"),
    ("label", "étiquettes (formats de texte sans réglage)", ("étiquette", "format de texte")),
    ("render", "problème de rendu", ("non traduit", "presque jointif", "pictogramme", "écart au rendu QGIS")),
    ("font-mark", "marque de police", "marque de police"),
    ("no-plate", "sans vignette du catalogue", "pas de vignette"),
]


def problem_type(message):
    """Clé du type de problème d'un message (« other » si aucun ne correspond)."""
    for key, _, patterns in PROBLEM_TYPES:
        if any(p in message for p in ((patterns,) if isinstance(patterns, str) else patterns)):
            return key
    return "other"


def filter_options(counts, total):
    """Options du filtre de la planche, avec le nombre de fiches de chaque type."""
    options = [
        ("all", f"toutes les fiches ({total})"),
        ("any", f"avec un problème ou une remarque ({counts.get('any', 0)})"),
    ]
    options += [(key, f"{label} ({counts[key]})") for key, label, _ in PROBLEM_TYPES if counts.get(key)]
    if counts.get("other"):
        options.append(("other", f"autres ({counts['other']})"))
    return "".join(f'<option value="{key}">{html.escape(label)}</option>' for key, label in options)


def problems(key, scores, issues, script):
    """Problèmes d'un code : diagnostics de convert_symbols + écart au rendu QGIS → [{text, files}].
    L'écart n'est affiché que s'il est la seule erreur « ⚠ » du code : sinon, il découle le plus souvent de
    l'erreur déjà signalée (étiquette, police, motif jointif…)."""
    out = [i if isinstance(i, dict) else {"text": i, "files": []} for i in issues.get(key, [])]
    d = scores.get(key)
    if d is not None and d > DEVIATION and not any(i["text"].startswith("⚠") for i in out):
        entry = script.get(key, {})
        files = [[entry["source"], "symbole QGIS (rendu de référence)"]] if entry.get("source") else []
        if entry.get("file"):
            files.append([f"data/symbols/script/{entry['file']}", "SLD produit (sg5-to-gpu)"])
        out.append({"text": f"⚠ écart au rendu QGIS : {d:.1f}", "files": files})
    return out


def no_plate(entries):
    """Remarque « pas de vignette » d'une fiche : catalogue PDF et fiche attendus."""
    e = entries[0]
    folder = e["domain"].upper()
    if e["domain"] == "sup":
        label = e["code"]
    elif e["layer"] == "zone_urba":
        label = e["attrs"].get("typezone") or e["code"]
    else:
        label = e.get("etiquette") or e["code"]
    return {
        "text": "ℹ pas de vignette dans le catalogue CNIG",
        "files": [
            [
                f"{folder}/Catalogue_Symbolisation_CNIG_{folder}.pdf",
                f"fiche « {label} » attendue (ou plage de fiche qui la couvre)",
            ]
        ],
    }


FILES_ICON = (
    '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M4 1.5h5l3 3v10H4z M9 1.5v3h3" '
    'fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>'
)


def _files_html(files):
    """Infobulle des fichiers attendus ou en cause d'un problème (chemins du dépôt CNIG, sauf data/…)."""
    if not files:
        return ""
    rows = "".join(
        f"<span><code>{html.escape(path)}</code>{f' — {html.escape(role)}' if role else ''}</span>"
        for path, role in files
    )
    return (
        f'<span class="files" tabindex="0" aria-label="fichiers concernés">{FILES_ICON}'
        f'<span class="tip" role="tooltip">{rows}</span></span>'
    )


def _problem_html(items, geometry=""):
    """Éléments de liste des remarques : pastille « ⚠ » / « ℹ », préfixe de géométrie, texte, fichiers en
    infobulle."""
    out = []
    for item in items:
        icon, _, text = item["text"].partition(" ")
        css = "w" if icon == "⚠" else "i"
        out.append(
            f'<li class="{css}"><span class="ico">{icon}</span><div>{geometry}{html.escape(text)}'
            f"{_files_html(item.get('files'))}</div></li>"
        )
    return "".join(out)


GEOM_CODE = re.compile(r"\b(PSC|INF)-([SLP])_")


def grouped_problems(found_items, sheet):
    """Problèmes d'une fiche regroupés quand ils ne diffèrent que par la géométrie du code (PSC-S_… / PSC-P_…) :
    [(préfixe de géométrie HTML, item)], texte du 1er code avec les géométries réunies (PSC-S/P_40-50…)."""
    groups = {}
    for geom, item in found_items:
        key = GEOM_CODE.sub(r"\1-#_", item["text"])
        g = groups.setdefault(key, {"geoms": [], "item": item, "files": []})
        if geom and geom not in g["geoms"]:
            g["geoms"].append(geom)
        g["files"] += [f for f in item.get("files", []) if f not in g["files"]]
    out = []
    for key, g in groups.items():
        geoms = sorted(g["geoms"], key="SLP".index)
        text = key.replace("-#_", f"-{'/'.join(geoms) or '?'}_") if "-#_" in key else g["item"]["text"]
        prefix = f'<span class="geom">{", ".join(GEOM_LETTER[x] for x in geoms)}</span>' if sheet and geoms else ""
        out.append((prefix, {**g["item"], "text": text, "files": g["files"]}))
    return out


def plate(name):
    """Cellule du tableau : vignette du catalogue CNIG (ou « — »)."""
    if not name:
        return '<td class="render"><div class="na">—</div></td>'
    return f'<td class="render"><div class="plate"><img src="cnig/{html.escape(name)}" alt=""></div></td>'


def source_tag(entry):
    """Pastille d'un code sans symbole propre dans le style XML CNIG : « déduit » ou « aucun »."""
    source = (entry or {}).get("source", "")
    if source.startswith("="):
        title = f"code absent du style XML : symbole de {html.escape(source[2:])}"
        return f'<span class="src d" title="{title}">déduit</span>'
    if not entry:
        return '<span class="src d" title="aucun style CNIG">aucun</span>'
    return ""


TRANSPARENT_TITLE = (
    "variante « transparent » de la grille : symbole invisible dans le style XML, aucune règle dans les SLD "
    "(objet non dessiné)"
)


def transparent_by_group(index, sections):
    """Variantes « transparent » (_000) rattachées à la 1re fiche de même type et sous-type : {(section, clé de
    fiche) : [entrées]} (sans fiche du même type : non affichées)."""
    first = {}
    for name, sec in sections.items():
        for g in sec["groups"]:
            first.setdefault((name, g["key"].rsplit("_", 1)[0]), g["key"])
    out = {}
    for e in index.get("transparent", []):
        a = e["attrs"]
        tkey = f"{a.get('typepsc') or a.get('typeinf')}-{a.get('stypepsc') or a.get('stypeinf')}"
        sec_name = f"{e['domain']}_{e['layer'].split('_')[0]}"
        gkey = first.get((sec_name, tkey))
        if gkey is not None:
            out.setdefault((sec_name, gkey), []).append(e)
    return out


def sheet_row(name, sec, group, images, plates, scores, issues, context, transparent=()):
    """Ligne HTML d'une fiche → (html, remarques « ⚠ » / « ℹ » de la fiche, SVG chargés par ses styles).
    transparent : variantes « transparent » (_000) du même type et sous-type, listées sous les codes."""
    entries = [(layer, group["codes"][layer]) for layer in sec["layers"] if layer in group["codes"]]
    first = entries[0][1]

    def geometry(layer):
        """Étiquette « surf. », « lin. », « pct. » des fiches à plusieurs géométries."""
        return f'<span class="geom">{GEOM_LETTER[geom_of(layer)]}</span>' if sec["sheet"] else ""

    keys = [f"{row['domain']}:{row['code']}" for _, row in entries]
    codes = "".join(
        f"<div>{geometry(layer)}<code>{html.escape(row['code'])}</code>{source_tag(context[1].get(key))}</div>"
        for (layer, row), key in zip(entries, keys, strict=True)
    ) + "".join(
        f"<div>{geometry(t['layer'])}<code>{html.escape(t['code'])}</code>"
        f'<span class="src tr" title="{html.escape(TRANSPARENT_TITLE)}">transp.</span></div>'
        for t in transparent
    )
    vignette = next((plates[key] for key in keys if key in plates), None)

    index_entries, script = context
    svgs = fiche_svgs(keys, script)
    detail, found, found_items = [], [], []
    for layer, key in [(layer, k) for (layer, _), k in zip(entries, keys, strict=True)] + [
        (t["layer"], f"{t['domain']}:{t['code']}") for t in transparent
    ]:
        found_items += [(geom_of(layer), item) for item in problems(key, scores, issues, script)]
    for prefix, item in grouped_problems(found_items, sec["sheet"]):
        detail.append(_problem_html([item], prefix))
        found.append(item["text"])
    if vignette is None:
        missing = no_plate([index_entries[key] for key in keys])
        detail.append(_problem_html([missing]))
        found.append(missing["text"])

    notes = f"<ul>{''.join(detail)}</ul>" if detail else '<span class="ok">aucune</span>'
    cells = [
        f'<td class="codes">{codes}<span class="cls">{html.escape(first["classement"])}</span></td>',
        f'<td class="lab">{html.escape(first["label"])}</td>',
        plate(vignette),
        *(
            tile(images.get((name, v)), group["cell"], badge=svg_badge(svgs) if v == "script" else "")
            for v in MAIN_VARIANTS
        ),
        f'<td class="pb">{notes}</td>',
        tile(images.get((name, "prod")), group["cell"], "render prod"),
    ]
    types = sorted({problem_type(message) for message in found})
    opening = f'<tr data-types="{" ".join(types)}">' if types else "<tr>"
    return opening + "".join(cells) + "</tr>", found, svgs


def write_html(sections, images, scores, sha):
    """Planche report/compare.html : un seul tableau, une section par couche, une ligne par fiche.
    → nombre de fiches {"all", "warn" (avec « ⚠ »), "note" (avec « ⚠ » ou « ℹ »)}.

    Le filtre de la page propose chaque type de problème présent, avec son nombre de fiches."""
    plates = read_json(REPORT_DIR / "cnig/plates.json", {})
    issues = read_json(ISSUES, {})
    cnig_index = read_json(CNIG_INDEX)
    context = (
        {f"{e['domain']}:{e['code']}": e for e in cnig_index["entries"]},
        read_json(SCRIPT_DIR / "index.json", {}),
    )
    transparent = transparent_by_group(cnig_index, sections)
    ncols = 4 + len(VARIANTS)
    html_sections, nav = [], []
    totals = {"all": 0, "warn": 0, "note": 0}
    all_svgs = set()  # SVG chargés par les styles script de la planche
    type_counts = {}  # type de problème (et « any ») → nombre de fiches
    for name, sec in sections.items():
        rows, n_warn, n_note, section_svgs = [], 0, 0, set()
        for group in sec["groups"]:
            row, found, svgs = sheet_row(
                name, sec, group, images, plates, scores, issues, context, transparent.get((name, group["key"]), [])
            )
            section_svgs.update(svgs)
            rows.append(row)
            n_warn += any(p.startswith("⚠") for p in found)
            n_note += bool(found)
            for key in {problem_type(message) for message in found} | ({"any"} if found else set()):
                type_counts[key] = type_counts.get(key, 0) + 1
        n = len(sec["groups"])
        totals["all"] += n
        totals["warn"] += n_warn
        totals["note"] += n_note
        all_svgs |= section_svgs
        label = SECTION_TITLES.get(name, name)
        nav.append(f'<a href="#{name}" data-section="{name}">{label}</a>')
        title = (
            f"<h2>{label}</h2><span class=stat>{n} fiches</span>"
            f'<span class="stat{" w" if n_warn else ""}">{n_warn} avec ⚠</span>'
            f'<span class="stat">{n_note} avec remarque</span>'
            f'<span class="stat">{svg_summary(section_svgs)}</span>'
        )
        html_sections.append(
            f'<tbody data-section="{name}"><tr class="sec" id="{name}"><td colspan="{ncols}">{title}</td></tr>'
            + "".join(rows)
            + "</tbody>"
        )
    (REPORT_DIR / "compare.html").write_text(
        PAGE.substitute(
            tile=TILE,
            sha=sha,
            svgs=svg_summary(all_svgs),
            scale=int(RES / 0.00028),
            nav=" ".join(nav),
            filters=filter_options(type_counts, totals["all"]),
            sections="\n".join(html_sections),
        ),
        encoding="utf-8",
    )
    return totals


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sections", help="sous-ensemble de sections (ex. plu_prescription,sup)")
    ap.add_argument("--skip-qgis", action="store_true")
    args = ap.parse_args()

    layers, sections = build_sections()
    if args.sections:
        sections = {k: v for k, v in sections.items() if k in args.sections.split(",")}
        keep = {layer for s in sections.values() for layer in s["layers"]}
        layers = {k: v for k, v in layers.items() if k in keep}
    size_of = {layer: s["n"] for s in sections.values() for layer in s["layers"]}
    write_gpkg(layers)
    print(f"✅ {GPKG.relative_to(ROOT)} : {sum(map(len, layers.values()))} entités, {len(layers)} couches")

    styles = {}
    for name, rows in layers.items():
        for variant, text in (("prod", prod_style(name)), ("script", script_style(name, rows))):
            if text:
                styles[f"{variant}_{name}"] = text
    install_resources()
    publish(styles)
    print(f"✅ GeoServer {GEOSERVER_CMP_WORKSPACE} : {len(styles)} styles")

    img_dir = REPORT_DIR / "img"
    shutil.rmtree(img_dir, ignore_errors=True)
    img_dir.mkdir(parents=True)
    jobs = []
    for name in layers:
        box, size = bbox(size_of[name])
        for variant in ("prod", "script"):
            style = f"{variant}_{name}"
            if style in styles:
                getmap(name, style, box, size, img_dir / f"{style}.png")
        domain = name.partition("_")[0]
        xml = CNIG_DIR / f"{domain.upper()}/QGIS/{domain.upper()}_CNIG.xml"
        if xml.is_file() and not args.skip_qgis:
            jobs.append(
                {
                    "gpkg": str(GPKG),
                    "layer": name,
                    "style": str(xml),
                    "bbox": box,
                    "size": size,
                    "out": str(img_dir / f"qgis_{name}.png"),
                    "fonts": [str(FONTS_DIR)],
                }
            )
    if jobs:
        jobs_file = REPORT_DIR / "qgis_jobs.json"
        write_json(jobs_file, jobs)
        subprocess.run([QGIS_PYTHON, str(ROOT / "bin/render_qgis.py"), str(jobs_file)], check=True)

    scores = compute_scores(layers, img_dir)
    flagged = sum(1 for d in scores.values() if d > DEVIATION)
    print(
        f"✅ écarts au rendu QGIS : {len(scores)} codes mesurés, {flagged} au-delà de {DEVIATION:g} → "
        f"{SCORES.relative_to(ROOT)}"
    )

    images = {}
    for name, sec in sections.items():
        for v in VARIANTS:
            out = img_dir / f"sec_{v}_{name}.png"
            if composite([img_dir / f"{v}_{layer}.png" for layer in sec["layers"]], out):
                images[(name, v)] = out.name

    totals = write_html(sections, images, scores, (read_json(CNIG_INDEX).get("sha") or "")[:7])
    print(
        f"✅ {(REPORT_DIR / 'compare.html').relative_to(ROOT)} : {totals['all']} fiches, {totals['warn']} avec « ⚠ », "
        f"{totals['note']} avec remarque"
    )


if __name__ == "__main__":
    main()
