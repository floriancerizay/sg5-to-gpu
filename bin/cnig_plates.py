#!/usr/bin/env python3
"""
2. Vignettes « rendu attendu » des catalogues PDF CNIG (<DOMAINE>/Catalogue_Symbolisation_CNIG_*.pdf),
rattachées aux codes de data/cnig_index.json :

  - fiches Prescriptions / Informations : vignette sous l'étiquette « tt-ss_nnn (SLP) », rattachée par la
    colonne `etiquette` des grilles CNIG (une fiche peut couvrir une plage de codes : « 15-00 à 98_001 ») ;
  - fiches Zonage : deux vignettes (≥ 1:2500 = _ge, < 1:2500 = _pe), une seule pour le PSMV ;
  - fiches SUP : dessin sous l'en-tête « A5|gen_XX|ass », rattaché aux « Symboles concernés ».

Sorties : report/cnig/*.png, report/cnig/plates.json ({"domaine:code": "fichier"}).
Prérequis : poppler-utils (pdftoppm, pdftotext).
"""
import re
import subprocess
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

from lxml import etree
from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CNIG_DIR, CNIG_INDEX, REPORT_DIR, read_json, write_json

OUT = REPORT_DIR / "cnig"
DPI = 150
NS = {"x": "http://www.w3.org/1999/xhtml"}
PDFS = {"plu": "PLU/Catalogue_Symbolisation_CNIG_PLU.pdf",
        "psmv": "PSMV/Catalogue_Symbolisation_CNIG_PSMV.pdf",
        "sup": "SUP/Catalogue_Symbolisation_CNIG_SUP.pdf"}

# Ligne de texte d'une page PDF, coordonnées en points (y vers le bas)
Line = namedtuple("Line", "x0 y0 x1 y1 text")


def pages_lines(pdf):
    """[(largeur de page, [Line])] pour chaque page."""
    xml = subprocess.run(["pdftotext", "-bbox-layout", str(pdf), "-"], capture_output=True, check=True).stdout
    root = etree.fromstring(xml, etree.XMLParser(recover=True, huge_tree=True))
    pages = []
    for page in root.iterfind(".//x:page", NS):
        lines = []
        for line in page.iterfind(".//x:line", NS):
            text = " ".join(w.text or "" for w in line.iterfind("x:word", NS))
            lines.append(Line(*(float(line.get(k)) for k in ("xMin", "yMin", "xMax", "yMax")), text))
        pages.append((float(page.get("width")), lines))
    return pages


def norm(label):
    """Étiquette de fiche sans espaces superflus ni suffixe de géométrie « (SLP) »."""
    return re.sub(r"\s*\((?:S|L|P|SL|SP|LP|SLP)\)\s*$", "", " ".join(label.split()))


def parse_label(label):
    """« 16-00 à 02 & 04 à 05_001 » → ("16", "001", [(0, 2), (4, 5)]) ; None si non reconnu."""
    m = re.match(r"^(\d{2})-(.+)_(\d{3})$", label.replace(" ", ""))
    if not m:
        return None
    typ, body, nnn = m.groups()
    ranges = []
    for part in body.split("&"):
        bounds = [b for b in re.split(r"-?à", part.strip("-")) if b]
        if not bounds or not all(b.isdigit() for b in bounds):
            return None
        ranges.append((int(bounds[0]), int(bounds[-1])))
    return typ, nnn, ranges


def label_covers(label, etiquette):
    """La fiche « label » couvre-t-elle l'étiquette de grille « tt-ss_nnn » ?"""
    sheet, code = parse_label(label), parse_label(etiquette)
    if not sheet or not code or sheet[:2] != code[:2]:
        return False
    subtype = code[2][0][0]
    return any(lo <= subtype <= hi for lo, hi in sheet[2])


def crop(img, box, margin=3):
    """Découpe une zone (en points) de la page, réduite à son contenu non blanc ; None si vide."""
    k = DPI / 72
    part = img.crop(tuple(int(v * k) for v in box)).convert("RGB")
    white = Image.new("RGB", part.size, (255, 255, 255))
    bbox = ImageChops.difference(part, white).convert("L").point(lambda v: 255 if v > 12 else 0).getbbox()
    if not bbox:
        return None
    m = int(margin * k)
    return part.crop((max(0, bbox[0] - m), max(0, bbox[1] - m), bbox[2] + m, bbox[3] + m))


def page_title(lines):
    """« Prescriptions PLU », « Zonage PSMV », « Servitudes d'Utilité Publique »… (2e ligne du cartouche)."""
    tops = sorted((l for l in lines if l.y0 < 70 and 120 < l.x0 < 420), key=lambda l: l.y0)
    return " ".join(l.text for l in tops)


def sheet_header(lines, width, sup=False):
    """Étiquette de la fiche : plus grande ligne du bandeau à droite (SUP : plus grande ligne « X|gen|ass »)."""
    if sup:
        candidates = [l for l in lines if "|" in l.text]
    else:
        candidates = [l for l in lines if 45 < l.y0 < 160 and l.x1 > width * 0.55 and l.y1 - l.y0 > 13]
    return max(candidates, key=lambda l: l.y1 - l.y0) if candidates else None


def plates_psc_inf(img, lines, head):
    """Fiche prescription / information : vignette entre l'étiquette, le panneau de droite et le symbole ponctuel."""
    key = norm(head.text)
    label = next((l for l in lines if l.x0 < 300 and l.y0 > 60 and norm(l.text).startswith(key[:5])), None)
    if not label:
        return {}
    panel_x = min((l.x0 for l in lines if l.text.startswith(("Symbole linéaire", "Symbole surfacique"))),
                  default=label.x0 + 200)
    point_y = min((l.y0 for l in lines if l.text.startswith("Symbole ponctuel") and l.y0 > label.y0),
                  default=label.y0 + 160)
    return {key: crop(img, (label.x0 - 8, label.y0 - 6, panel_x - 6, point_y - 8))}


def plates_zone(img, lines, head):
    """Fiche de zonage : une vignette par étiquette de cadre (deux en PLU : _ge puis _pe)."""
    key = norm(head.text)
    # étiquettes de cadre (petite police) ; exclut le libellé dessiné dans la carte
    labels = sorted((l for l in lines if l.x0 < 200 and l.y0 > 80 and l.text.strip() == key and l.y1 - l.y0 < 14),
                    key=lambda l: l.y0)
    desc_x = min((l.x0 for l in lines if l.text.startswith(("***", "Ligne", "Remplissage"))), default=180)
    out = {}
    for i, label in enumerate(labels):
        bottom = labels[i + 1].y0 - 8 if i + 1 < len(labels) else label.y0 + 170
        suffix = ("_ge", "_pe")[i] if len(labels) == 2 else ""
        out[key + suffix] = crop(img, (15, label.y0 - 6, desc_x - 6, bottom))
    return out


def plates_sup(img, lines, head, width):
    """Fiche SUP : dessin sous l'en-tête, partagé par l'en-tête et les « Symboles concernés »."""
    below = [l for l in lines if l.y0 > head.y1 + 15 and l.x0 < 100]
    bottom = min((l.y0 for l in below), default=head.y1 + 200)
    picture = crop(img, (20, head.y1 + 4, width - 20, bottom - 6))
    codes = [norm(head.text)]
    listed = False
    for l in sorted(lines, key=lambda l: l.y0):
        if l.text.startswith("Symboles concernés"):
            listed = True
        elif listed and "|" in l.text:
            codes += l.text.split()
    return {c: picture for c in codes}


def extract(domain, pdf):
    """Vignettes d'un catalogue, écrites dans OUT → {(genre, étiquette): nom du fichier}."""
    found = {}
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-r", str(DPI), "-png", str(pdf), f"{tmp}/p"], check=True)
        images = sorted(Path(tmp).glob("p-*.png"))
        for (width, lines), img_path in zip(pages_lines(pdf), images):
            head = sheet_header(lines, width, sup=domain == "sup")
            if not head:
                continue
            kind = page_title(lines).lower()
            img = Image.open(img_path)
            if domain == "sup":
                genre, plates = "sup", plates_sup(img, lines, head, width)
            elif "zonage" in kind:
                genre, plates = "zone", plates_zone(img, lines, head)
            elif "prescription" in kind:
                genre, plates = "psc", plates_psc_inf(img, lines, head)
            elif "information" in kind:
                genre, plates = "inf", plates_psc_inf(img, lines, head)
            else:
                continue
            for key, picture in plates.items():
                if picture is None:
                    continue
                name = f"{domain}_{genre}_{re.sub(r'[^A-Za-z0-9_-]+', '_', key)}.png"
                picture.save(OUT / name)
                found[(genre, key)] = name
    return found


def plate_key(e, found):
    """Clé (genre, étiquette) de la vignette d'une entrée d'index, parmi les vignettes trouvées."""
    if e["domain"] == "sup":
        return "sup", e["code"]
    if e["layer"] == "zone_urba":
        key = ("zone", e["attrs"]["typezone"] + ("_" + e["scale"] if e.get("scale") else ""))
        return key if key in found else ("zone", e["attrs"]["typezone"])
    genre = "psc" if e["layer"].startswith("prescription") else "inf"
    key = (genre, norm(e.get("etiquette") or ""))
    if key in found:
        return key
    return next((f for f in found if f[0] == genre and label_covers(f[1], key[1])), key)


def main():
    index = read_json(CNIG_INDEX)
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.png"):
        f.unlink()
    mapping = {}
    for domain, rel in PDFS.items():
        found = extract(domain, CNIG_DIR / rel)
        attached = 0
        for e in index["entries"]:
            if e["domain"] != domain:
                continue
            key = plate_key(e, found)
            if key in found:
                mapping[f"{domain}:{e['code']}"] = found[key]
                attached += 1
        print(f"✅ {domain} : {len(found)} vignettes extraites, {attached} codes rattachés")
    write_json(OUT / "plates.json", mapping)


if __name__ == "__main__":
    main()
