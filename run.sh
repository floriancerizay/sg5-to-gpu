#!/usr/bin/env bash
# Styles GeoServer des symboles CNIG : conversion des symboles QGIS du CNIG (bin/qgis2sld.py), rendu GeoServer / QGIS,
# SLD par couche GPU (data/slds), planche report/compare.html et problèmes du symbole d'origine
# (data/symbols/issues.json). Détail des étapes : README.md.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
cd "$SCRIPT_DIR"

# Surcharges locales (GEOSERVER_URL…) : .env, voir .env.example ; les variables déjà exportées priment
if [ -f .env ]; then
  while IFS='=' read -r k v; do
    k="${k//[[:space:]]/}"
    [[ -z "$k" || "$k" == \#* ]] && continue
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    if [ -z "${!k+x}" ]; then export "$k=$v"; fi
  done < .env
fi

PYTHON="${PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
QGIS_PYTHON="${QGIS_PYTHON:-/usr/bin/python3}"
GEOSERVER_URL="${GEOSERVER_URL:-http://localhost:8080/geoserver}"
CNIG_DIR="${CNIG_DIR:-$PROJECT_ROOT/cnig}"
export PROJECT_ROOT QGIS_PYTHON GEOSERVER_URL CNIG_DIR

usage() {
  cat >&2 <<EOF
Usage : $0 [options] [-- options de bin/compare.py]

  --from ÉTAPE     reprendre à une étape : index | plates | convert | slds | compare
  --validate       étape slds : publication d'essai des SLD assemblés sur le GeoServer local
  --sections S     sous-ensemble de sections de la planche (ex. plu_prescription,sup)
  --skip-qgis      sans rendu QGIS (essai rapide : pas de mesure, scores.json réécrit)
  -h, --help       cette aide

Étapes : index (bin/cnig_index.py) → plates (bin/cnig_plates.py) → convert (bin/convert_symbols.py)
         → slds (bin/build_slds.py) → compare (bin/compare.py)
EOF
  exit "${1:-1}"
}

STEPS=(index plates convert slds compare)
FROM=index
SLDS_ARGS=()
COMPARE_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM="${2:?}"; shift 2 ;;
    --sections) COMPARE_ARGS+=(--sections "${2:?}"); shift 2 ;;
    --skip-qgis) COMPARE_ARGS+=(--skip-qgis); shift ;;
    --validate) SLDS_ARGS+=(--validate); shift ;;
    -h|--help) usage 0 ;;
    --) shift; COMPARE_ARGS+=("$@"); break ;;
    *) usage ;;
  esac
done
[[ " ${STEPS[*]} " == *" $FROM "* ]] || usage

# Vrai si l'étape $1 doit être exécutée (à partir de --from)
run_step() {
  local s
  for s in "${STEPS[@]}"; do
    [ "$s" = "$FROM" ] && return 0
    [ "$s" = "$1" ] && return 1
  done
}

# --- Prérequis ---------------------------------------------------------------------------------
fail() { echo "❌ $*" >&2; exit 1; }
[ -x "$PYTHON" ] || fail "$PYTHON absent : python3 -m venv $PROJECT_ROOT/.venv puis $PROJECT_ROOT/.venv/bin/pip install -r requirements.txt"
"$PYTHON" -c "import lxml, numpy, PIL, scipy, fontTools" 2>/dev/null \
  || fail "dépendances manquantes dans $PYTHON : (cd $SCRIPT_DIR && $PYTHON -m pip install -r requirements.txt)"
[ -n "$(ls -A "$CNIG_DIR" 2>/dev/null)" ] \
  || fail "dépôt CNIG absent de $CNIG_DIR : git submodule update --init"
[ -n "${GPU_DIR:-}" ] || fail "GPU_DIR non défini : chemin d'un clone de gpu-entrepot, à indiquer dans .env (voir .env.example)"
[ -d "$GPU_DIR/STYLES/data/du" ] && [ -d "$GPU_DIR/COUCHES" ] \
  || fail "GPU_DIR=$GPU_DIR : STYLES/data/du ou COUCHES absent, ce n'est pas un clone de gpu-entrepot"
command -v ogr2ogr >/dev/null || fail "ogr2ogr absent (gdal-bin)"
command -v pdftoppm >/dev/null || fail "pdftoppm absent (poppler-utils)"
if run_step compare || [ ${#SLDS_ARGS[@]} -gt 0 ]; then
  curl -sf -o /dev/null "$GEOSERVER_URL/web/" || fail "GeoServer injoignable sur $GEOSERVER_URL (démarrer : /usr/share/geoserver/bin/startup.sh)"
  if [[ " ${COMPARE_ARGS[*]-} " != *" --skip-qgis "* ]]; then
    "$QGIS_PYTHON" -c "import qgis.core" 2>/dev/null || fail "$QGIS_PYTHON n'a pas accès à PyQGIS (QGIS_PYTHON=… ou --skip-qgis)"
  fi
fi
echo "CNIG : $CNIG_DIR ($(git -C "$CNIG_DIR" log -1 --format='%h %cs' 2>/dev/null || echo 'hors git'))"
echo "GPU  : $GPU_DIR ($(git -C "$GPU_DIR" log -1 --format='%h %cs' 2>/dev/null || echo 'hors git'))"

# --- 1. Index des codes CNIG -------------------------------------------------------------------
if run_step index; then
  echo "== 1/5 index des symboles CNIG"
  "$PYTHON" bin/cnig_index.py
fi

# --- 2. Vignettes « attendu » des catalogues PDF -----------------------------------------------
if run_step plates; then
  echo "== 2/5 vignettes des catalogues PDF CNIG"
  "$PYTHON" bin/cnig_plates.py
fi

# --- 3. Bibliothèque de symboles --------------------------------------------------------
if run_step convert; then
  echo "== 3/5 conversion des symboles CNIG (styles XML) en SLD"
  "$PYTHON" bin/convert_symbols.py
fi

# --- 4. SLD par couche GPU (classiques / par symbole) -------------------------------------------
if run_step slds; then
  echo "== 4/5 assemblage des SLD par couche (data/slds)"
  "$PYTHON" bin/build_slds.py ${SLDS_ARGS[@]+"${SLDS_ARGS[@]}"}
fi

# --- 5. Rendus, écarts, planche --------------------------------------------------------
echo "== 5/5 rendus GeoServer / QGIS et planche"
"$PYTHON" bin/compare.py ${COMPARE_ARGS[@]+"${COMPARE_ARGS[@]}"}
echo "Planche : $SCRIPT_DIR/report/compare.html"
