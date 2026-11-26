#!/usr/bin/env bash
# Montée de version du dépôt CNIG (sous-module cnig/) : liste des codes ajoutés / modifiés / supprimés
# (data/cnig_diff.json), puis ./run.sh pour régénérer la planche.
#
# Usage : ./bump.sh [SHA] [-- options de run.sh]
#   sans SHA : dernier commit de la branche suivie (SYMBOLISATION, voir .gitmodules)
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-.venv/bin/python}"

SHA=""
if [ $# -gt 0 ] && [ "$1" != "--" ]; then SHA="$1"; shift; fi
[ "${1:-}" = "--" ] && shift

[ -e cnig/.git ] || git submodule update --init cnig
OLD="$(git -C cnig rev-parse HEAD)"

# Index de la version actuelle, base de la liste des codes modifiés
if [ ! -f "data/cnig_index.${OLD:0:7}.json" ]; then
  echo "== index de la version actuelle ${OLD:0:7}"
  "$PYTHON" bin/cnig_index.py >/dev/null
fi

if [ -z "$SHA" ]; then
  git submodule update --remote cnig
else
  git -C cnig fetch --depth 1 origin "$SHA"
  git -C cnig checkout -q FETCH_HEAD
fi
NEW="$(git -C cnig rev-parse HEAD)"
[ "$NEW" != "$OLD" ] || { echo "Dépôt CNIG déjà à ${OLD:0:7}"; exit 0; }
echo "Dépôt CNIG : ${OLD:0:7} → ${NEW:0:7}"

trap 'echo "❌ échec : retour à ${OLD:0:7} par git submodule update cnig" >&2' ERR
"$PYTHON" bin/cnig_index.py --since "$OLD"
./run.sh --from plates "$@"

echo
echo "À commiter ensemble : cnig data/cnig_index.${NEW:0:7}.json report/"
