#!/usr/bin/env bash
# Clone (or update) the Surveyor target repositories into ~/scan.
#
# Usage:
#   ./scripts/clone-targets.sh                 # clone/update them all (many GB, long)
#   ./scripts/clone-targets.sh kafka redis     # only the named targets
#   ./scripts/clone-targets.sh --list          # list target names, then exit
#   SCAN_DIR=/data/scan ./scripts/clone-targets.sh
#
# If a repo is already present it is UPDATED (git pull --ff-only); otherwise it is
# cloned with FULL history. Do NOT shallow- or blobless-clone: Surveyor reads every
# file version via `git cat-file`, and offline runs would break. Safe to re-run.
set -u

SCAN_DIR="${SCAN_DIR:-$HOME/scan}"

# name              org/repo                            # category / note
REPOS=(
  # --- Java (best bug-fix ground truth; on-theme) ---
  "kafka            apache/kafka"                       # JIRA keys; concentrated core
  "cassandra        apache/cassandra"                   # god-classes; strong positive
  "jenkins          jenkinsci/jenkins"                  # legacy sprawl; positive
  "hibernate-orm    hibernate/hibernate-orm"            # HHH- JIRA; complex core
  "camel            apache/camel"                       # huge; many components
  "spring-framework spring-projects/spring-framework"   # control (well-factored)
  "guava            google/guava"                       # control (clean)
  # --- C# / .NET ---
  "nopcommerce      nopSolutions/nopCommerce"           # positive (e-commerce sprawl)
  "roslyn           dotnet/roslyn"                       # big; complex compiler
  "jellyfin         jellyfin/jellyfin"                  # medium real app
  # --- JavaScript / TypeScript ---
  "kibana           elastic/kibana"                     # positive (sprawl); big
  "vscode           microsoft/vscode"                   # big; good issue hygiene
  "react            facebook/react"                     # control-ish
  # --- Python ---
  "home-assistant   home-assistant/core"                # positive (contributor sprawl)
  "airflow          apache/airflow"                     # large
  "django           django/django"                      # control-ish
  # --- Go ---
  "moby             moby/moby"                           # positive (legacy Docker)
  "prometheus       prometheus/prometheus"               # control-ish
  "kubernetes       kubernetes/kubernetes"               # big; great issues
  # --- C ---
  "redis            redis/redis"                         # control (clean C)
  "git              git/git"                             # medium
)

if [[ "${1:-}" == "--list" ]]; then
  printf '%s\n' "${REPOS[@]}" | awk '{printf "  %-16s https://github.com/%s\n", $1, $2}'
  exit 0
fi

command -v git >/dev/null || { echo "error: git not on PATH" >&2; exit 1; }
mkdir -p "$SCAN_DIR"

want=("$@")
in_want() {
  [[ ${#want[@]} -eq 0 ]] && return 0
  local w; for w in "${want[@]}"; do [[ "$w" == "$1" ]] && return 0; done
  return 1
}

if [[ ${#want[@]} -eq 0 ]]; then
  echo "Clone/update ALL ${#REPOS[@]} targets in $SCAN_DIR (full history — many GB, can"
  echo "take a long time). Pass names for a subset, or --list. Ctrl-C to abort."
  echo
fi

cloned=0; updated=0; fail=0; failed=()
for entry in "${REPOS[@]}"; do
  set -- $entry; name="$1"; slug="$2"
  in_want "$name" || continue
  dest="$SCAN_DIR/$name"
  url="https://github.com/$slug.git"
  if [[ -d "$dest/.git" ]]; then
    echo "~ update $name"
    if git -C "$dest" pull --ff-only --quiet; then
      updated=$((updated + 1))
    else
      echo "! FAILED pull $name (diverged? try: git -C $dest pull --rebase)"
      fail=$((fail + 1)); failed+=("$name")
    fi
  else
    echo "+ clone  $name  <-  $url"
    if git clone --progress "$url" "$dest"; then
      cloned=$((cloned + 1))
    else
      echo "! FAILED clone $name"
      fail=$((fail + 1)); failed+=("$name")
      rm -rf "$dest"   # drop half-finished clone so a re-run retries cleanly
    fi
  fi
done

echo
echo "done: $cloned cloned, $updated updated, $fail failed  (in $SCAN_DIR)"
if ((fail)); then echo "failed: ${failed[*]}"; exit 1; fi
echo "Next: ./scripts/scan-parallel.sh"
