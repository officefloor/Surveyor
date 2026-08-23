#!/usr/bin/env bash
# Remove Surveyor-GENERATED artifacts from ~/scan: the *.db files, the <repo>-report/
# and <repo>-report-split/ dirs, summary.md/csv, and the .surveyor scan work dir.
# Does NOT touch the cloned repositories themselves.
#
# Usage:
#   ./scripts/clean.sh                # remove ALL artifacts (prompts first)
#   ./scripts/clean.sh kafka redis    # only these repos' artifacts
#   ./scripts/clean.sh -n             # dry-run: list what would be removed
#   ./scripts/clean.sh -y             # skip the confirmation prompt
#   SCAN_DIR=/data/scan ./scripts/clean.sh
set -u

SCAN_DIR="${SCAN_DIR:-$HOME/scan}"
DRY=0; YES=0; names=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run) DRY=1; shift;;
    -y|--yes) YES=1; shift;;
    -h|--help) sed -n '2,13p' "$0"; exit 0;;
    *) names+=("$1"); shift;;
  esac
done

work="$SCAN_DIR/.surveyor"
targets=()
if [[ ${#names[@]} -gt 0 ]]; then
  for n in "${names[@]}"; do
    targets+=("$SCAN_DIR/$n.db" "$SCAN_DIR/$n-report" "$SCAN_DIR/$n-report-split"
              "$work/$n.progress" "$work/$n.status" "$work/logs/$n.log")
  done
else
  for f in "$SCAN_DIR"/*.db; do [[ -e "$f" ]] && targets+=("$f"); done
  for d in "$SCAN_DIR"/*-report "$SCAN_DIR"/*-report-split; do
    # defensive: never delete an actual git checkout that happens to match
    [[ -d "$d" && ! -d "$d/.git" ]] && targets+=("$d")
  done
  for f in "$SCAN_DIR/summary.md" "$SCAN_DIR/summary.csv"; do [[ -e "$f" ]] && targets+=("$f"); done
  [[ -d "$work" ]] && targets+=("$work")
fi

existing=()
for t in "${targets[@]}"; do [[ -e "$t" ]] && existing+=("$t"); done

if [[ ${#existing[@]} -eq 0 ]]; then
  echo "nothing to remove in $SCAN_DIR"; exit 0
fi

echo "Will remove ${#existing[@]} item(s) from $SCAN_DIR:"
for t in "${existing[@]}"; do echo "  $t"; done

if (( DRY )); then echo "(dry-run; nothing deleted)"; exit 0; fi
if (( ! YES )); then
  read -rp "Delete these? [y/N] " ans
  [[ "$ans" == [yY]* ]] || { echo "aborted"; exit 1; }
fi
rm -rf "${existing[@]}"
echo "removed ${#existing[@]} item(s)."
