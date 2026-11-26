"""Configuration commune des scripts bin/ : chemins, GeoServer de test, sources CNIG et GPU.

Les réglages peuvent être surchargés par une variable d'environnement ou par le fichier .env
(voir .env.example) ; une variable déjà exportée prime sur .env.
"""
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_env(path):
    """Charge les variables de `path` (KEY=valeur) sans écraser celles déjà définies."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_env(ROOT / ".env")

# --- Sorties ---
DATA_DIR = ROOT / "data"                     # sorties intermédiaires, non versionnées
CNIG_INDEX = DATA_DIR / "cnig_index.json"    # 1. index des codes CNIG
SVG_DIR = DATA_DIR / "svg"                   # 3. pictogrammes et tuiles
FONTS_DIR = DATA_DIR / "fonts"               # 3. polices des symboles et des étiquettes
SYMBOLS_DIR = DATA_DIR / "symbols"           # 3. diagnostics, 5. écarts au rendu QGIS
SCRIPT_DIR = SYMBOLS_DIR / "script"          # 3. un SLD par code
SLDS_DIR = DATA_DIR / "slds"                 # 4. SLD par couche GPU (livrable)
GPKG = DATA_DIR / "layer/compare.gpkg"       # 5. grille de test
REPORT_DIR = ROOT / "report"                 # 2. vignettes, 5. planche : versionnée, publiée sur GitHub Pages

# --- GeoServer local de la planche ---
GEOSERVER_URL = os.getenv("GEOSERVER_URL", "http://localhost:8080/geoserver")
GEOSERVER_USER = os.getenv("GEOSERVER_USER", "admin")
GEOSERVER_PASSWORD = os.getenv("GEOSERVER_PASSWORD", "geoserver")
GEOSERVER_STYLES_IMG = os.getenv("GEOSERVER_STYLES_IMG", "/usr/share/geoserver/data_dir/styles/img")
GEOSERVER_FONTS = os.getenv("GEOSERVER_FONTS", str(Path(GEOSERVER_STYLES_IMG).parent / "fonts"))
GEOSERVER_CMP_WORKSPACE = os.getenv("GEOSERVER_CMP_WORKSPACE", "gpu_cmp")  # supprimé puis recréé à chaque passe
GEOSERVER_CMP_IMG = os.getenv("GEOSERVER_CMP_IMG", str(Path(GEOSERVER_STYLES_IMG).parent / "gpu_cmp_img"))

# Attribut géométrique des couches GPU (fonctions startPoint / startAngle des SLD)
GEOMETRY_NAME = os.getenv("GEOMETRY_NAME", "the_geom")

# Préfixe des images dans les SLD livrés (data/slds) : dossier distinct des images de production,
# car 24 pictos de production portent le nom d'un SVG CNIG au dessin différent
SLD_IMG_URL = os.getenv("SLD_IMG_URL", "https://data.geopf.fr/annexes/gpu/geoserver/images/cnig/")

# --- Production GPU : clone de gpu-entrepot (GPU_DIR, obligatoire ; relatif à la racine du dépôt) ---
#   STYLES/data/du, STYLES/data/sup     SLD de production : référence de la planche, règles conservées
#   STYLES/data/fonts                   pictogrammes et polices de production
#   STYLES/SLDS/notsup                  couches hors CNIG reprises telles quelles
#   STYLES/SLDS/sup/sup/assiette_sup_s.sld, COUCHES/CouchesWMS.csv, COUCHES/supByCategory.json : couches SUP
if not os.getenv("GPU_DIR"):
    raise SystemExit("GPU_DIR non défini : chemin d'un clone de gpu-entrepot, à indiquer dans .env (voir .env.example)")
GPU_DIR = (ROOT / os.path.expanduser(os.environ["GPU_DIR"])).resolve()
STYLES_DIR = GPU_DIR / "STYLES"
COUCHES_DIR = GPU_DIR / "COUCHES"
PROD_IMG_DIR = STYLES_DIR / "data/fonts"  # pictogrammes (mêmes noms que des SVG CNIG) et polices de production

# --- Dépôt CNIG DDU-SG5-SYMBOLISATION : sous-module git cnig/ (version : ./bump.sh) ---
CNIG_DIR = Path(os.getenv("CNIG_DIR", str(ROOT / "cnig")))
CNIG_GRILLES = CNIG_DIR / "__outils-agp-python-gdb__"

# Interpréteur ayant accès à PyQGIS (le .venv n'en hérite pas) : rendu de référence bin/render_qgis.py
QGIS_PYTHON = os.getenv("QGIS_PYTHON", "/usr/bin/python3")


def cnig_sha():
    """SHA du dépôt CNIG extrait dans CNIG_DIR, ou None hors git."""
    result = subprocess.run(["git", "-C", str(CNIG_DIR), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def read_json(path, default=None):
    """Contenu JSON de `path` ; `default` si le fichier est absent (sans `default`, le fichier est obligatoire)."""
    path = Path(path)
    if default is not None and not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
