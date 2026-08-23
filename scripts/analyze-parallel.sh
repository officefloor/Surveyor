#!/usr/bin/env bash
# Run `surveyor analyze` across many repos in parallel, one analysis per PHYSICAL
# CPU core (analysis is single-threaded, pure-Python and CPU-bound), with a live
# per-repo status board.
#
# Usage:
#   ./scripts/analyze-parallel.sh                 # analyze every scanned repo under ~/scan
#   ./scripts/analyze-parallel.sh kafka redis     # only these (names under SCAN_DIR, or paths)
#   ./scripts/analyze-parallel.sh -j 4            # force 4 concurrent analyses
#
# Env:
#   SCAN_DIR=~/scan            where the repos (and their <repo>.db) live
#   JOBS=N                     override the physical-core count
#   SURVEYOR="python -m surveyor"     override how surveyor is invoked
#   SURVEYOR_ANALYZE_ARGS="..."       extra args passed to each analyze
#                                     (e.g. --split-at 2022-01-01 --include-tests)
#
# Analyze reads <repo>.db and writes <repo>-report/ beside the checkout (Surveyor's
# convention). A repo with no <repo>.db yet is SKIPPED (scan it first). Per-repo
# console output goes to $SCAN_DIR/.surveyor/logs/<name>-analyze.log. Re-running
# overwrites each report.
set -u

SCAN_DIR="${SCAN_DIR:-$HOME/scan}"
EXTRA="${SURVEYOR_ANALYZE_ARGS:-}"
SURVEYOR="${SURVEYOR:-}"
if [[ -z "$SURVEYOR" ]]; then
  if command -v surveyor >/dev/null; then SURVEYOR="surveyor"; else SURVEYOR="python3 -m surveyor"; fi
fi

# ---- args ----
JOBS="${JOBS:-}"
names=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -j) JOBS="$2"; shift 2;;
    -j*) JOBS="${1#-j}"; shift;;
    -h|--help) sed -n '2,25p' "$0"; exit 0;;
    *) names+=("$1"); shift;;
  esac
done

# ---- physical core count (not logical/hyperthreads) ----
phys_cores() {
  local n=""
  if command -v lscpu >/dev/null 2>&1; then
    n=$(lscpu -p=CORE,SOCKET 2>/dev/null | grep -v '^#' | sort -u | wc -l)
  fi
  if [[ -z "$n" || "$n" -lt 1 ]] && [[ -r /proc/cpuinfo ]]; then
    n=$(awk -F: '/^physical id/{p=$2} /^core id/{print p":"$2}' /proc/cpuinfo | sort -u | wc -l)
  fi
  [[ -z "$n" || "$n" -lt 1 ]] && n=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
  echo "$n"
}
[[ -z "$JOBS" ]] && JOBS="$(phys_cores)"
[[ "$JOBS" -lt 1 ]] && JOBS=1

# ---- discover repos ----
repos=()
if [[ ${#names[@]} -gt 0 ]]; then
  for n in "${names[@]}"; do
    if [[ -d "$n/.git" ]]; then repos+=("${n%/}")
    elif [[ -d "$SCAN_DIR/$n/.git" ]]; then repos+=("$SCAN_DIR/$n")
    else echo "warn: skipping '$n' (not a git repo)"; fi
  done
else
  for d in "$SCAN_DIR"/*/; do [[ -d "${d}.git" ]] && repos+=("${d%/}"); done
fi
[[ ${#repos[@]} -eq 0 ]] && { echo "no git repos found in $SCAN_DIR"; exit 1; }

work="$SCAN_DIR/.surveyor"; mkdir -p "$work/logs"
sfile() { echo "$work/$(basename "$1").analyze.status"; }
lf() { echo "$work/logs/$(basename "$1")-analyze.log"; }
dbof() { echo "$1.db"; }   # <repo>.db beside the checkout (analyze's convention)

declare -A STATE PID
for r in "${repos[@]}"; do
  rm -f "$(sfile "$r")"
  if [[ -f "$(dbof "$r")" ]]; then STATE["$r"]=pending; else STATE["$r"]=skipped; fi
done

start_one() {
  local r="$1"
  ( if $SURVEYOR analyze "$r" $EXTRA >"$(lf "$r")" 2>&1; then
      echo ok > "$(sfile "$r")"; else echo fail > "$(sfile "$r")"; fi ) &
  PID["$r"]=$!
  STATE["$r"]=running
}

render_row() {
  local r="$1" name state tag
  name=$(basename "$r"); state=${STATE[$r]}
  case "$state" in
    done)    tag="done  ->  $(basename "$r")-report/";;
    failed)  tag="FAILED (logs/$name-analyze.log)";;
    running) tag="analyzing...";;
    skipped) tag="skipped (no $name.db — scan first)";;
    *)       tag="pending";;
  esac
  printf '  %-18s %-9s %s' "$name" "$state" "$tag"
}

TTY=0; [[ -t 1 ]] && TTY=1
NLINES=$(( 1 + ${#repos[@]} ))
drawn=0
draw() {
  local d=0 run=0 pend=0 f=0 sk=0 r
  for r in "${repos[@]}"; do case "${STATE[$r]}" in
    done) ((d++));; running) ((run++));; failed) ((f++));; skipped) ((sk++));; *) ((pend++));; esac; done
  if (( TTY )); then
    (( drawn )) && printf '\033[%dA' "$NLINES"
    printf 'Surveyor analyze | %d cores | %d repos: %d done, %d running, %d pending, %d skipped, %d failed\033[K\n' \
      "$JOBS" "${#repos[@]}" "$d" "$run" "$pend" "$sk" "$f"
    for r in "${repos[@]}"; do printf '%s\033[K\n' "$(render_row "$r")"; done
    drawn=1
  else
    printf '[%s] %d done, %d running, %d pending, %d skipped, %d failed\n' \
      "$(date +%H:%M:%S)" "$d" "$run" "$pend" "$sk" "$f"
  fi
}

echo "Analyzing ${#repos[@]} repos, $JOBS at a time ($SURVEYOR). Logs in $work/logs/"
[[ -n "$EXTRA" ]] && echo "extra analyze args: $EXTRA"
echo

next=0
draw
while :; do
  # reap finished
  for r in "${repos[@]}"; do
    if [[ "${STATE[$r]}" == running ]] && ! kill -0 "${PID[$r]}" 2>/dev/null; then
      wait "${PID[$r]}" 2>/dev/null
      [[ "$(cat "$(sfile "$r")" 2>/dev/null)" == ok ]] && STATE[$r]=done || STATE[$r]=failed
    fi
  done
  # top up to JOBS
  running_now=0
  for r in "${repos[@]}"; do [[ "${STATE[$r]}" == running ]] && ((running_now++)); done
  while (( running_now < JOBS )) && (( next < ${#repos[@]} )); do
    r="${repos[$next]}"; ((next++))
    [[ "${STATE[$r]}" == pending ]] && { start_one "$r"; ((running_now++)); }
  done
  draw
  # finished?
  remaining=0
  for r in "${repos[@]}"; do
    [[ "${STATE[$r]}" == pending || "${STATE[$r]}" == running ]] && ((remaining++))
  done
  (( remaining == 0 )) && break
  sleep 0.5
done
draw
echo

ok=0; bad=0; sk=0; badlist=()
for r in "${repos[@]}"; do
  case "${STATE[$r]}" in
    done) ok=$((ok + 1));;
    skipped) sk=$((sk + 1));;
    *) bad=$((bad + 1)); badlist+=("$(basename "$r")");;
  esac
done
echo "done: $ok analyzed, $sk skipped, $bad failed"
if ((bad)); then echo "failed: ${badlist[*]}  (see $work/logs/)"; exit 1; fi
echo "Reports: $SCAN_DIR/<name>-report/  (report.md, files.csv, coupling.csv, commits.html)"
