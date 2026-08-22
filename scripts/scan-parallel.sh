#!/usr/bin/env bash
# Run `surveyor scan` across many repos in parallel, one scan per PHYSICAL CPU core
# (scans are single-threaded and CPU-bound, so we avoid hyperthreads), with a live
# per-repo progress bar.
#
# Usage:
#   ./scripts/scan-parallel.sh                 # scan every git repo under ~/scan
#   ./scripts/scan-parallel.sh kafka redis     # only these (names under SCAN_DIR, or paths)
#   ./scripts/scan-parallel.sh -j 4            # force 4 concurrent scans
#
# Env:
#   SCAN_DIR=~/scan            where the repos live
#   JOBS=N                     override the physical-core count
#   SURVEYOR="python -m surveyor"   override how surveyor is invoked
#   SURVEYOR_SCAN_ARGS="..."   extra args passed to each scan (e.g. --max-commits 200)
#
# Each scan writes <repo>.db beside its checkout (Surveyor's convention). Per-repo
# console output goes to $SCAN_DIR/.surveyor/logs/<name>.log. Re-running resumes
# (scans skip commits already in the db).
set -u

SCAN_DIR="${SCAN_DIR:-$HOME/scan}"
EXTRA="${SURVEYOR_SCAN_ARGS:-}"
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
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
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
pf() { echo "$work/$(basename "$1").progress"; }
sfile() { echo "$work/$(basename "$1").status"; }
lf() { echo "$work/logs/$(basename "$1").log"; }

declare -A STATE PID
for r in "${repos[@]}"; do STATE["$r"]=pending; rm -f "$(pf "$r")" "$(sfile "$r")"; done

start_one() {
  local r="$1"
  ( if $SURVEYOR scan "$r" --progress-file "$(pf "$r")" $EXTRA >"$(lf "$r")" 2>&1; then
      echo ok > "$(sfile "$r")"; else echo fail > "$(sfile "$r")"; fi ) &
  PID["$r"]=$!
  STATE["$r"]=running
}

make_bar() {  # $1 = percent -> "[####----]"
  local pct=$1 width=26 filled empty f e
  filled=$(( pct * width / 100 )); (( filled > width )) && filled=$width
  empty=$(( width - filled ))
  printf -v f '%*s' "$filled" ''; printf -v e '%*s' "$empty" ''
  printf '[%s%s]' "${f// /#}" "${e// /-}"
}

render_row() {
  local r="$1" name state pf_ done_c=0 total_c=0 pct=0 tag
  name=$(basename "$r"); state=${STATE[$r]}; pf_=$(pf "$r")
  [[ -f "$pf_" ]] && read -r done_c total_c < "$pf_" 2>/dev/null
  [[ -z "$total_c" ]] && total_c=0; [[ -z "$done_c" ]] && done_c=0
  (( total_c > 0 )) && pct=$(( done_c * 100 / total_c ))
  case "$state" in
    done)    pct=100; tag="done";;
    failed)  tag="FAILED (logs/$name.log)";;
    running) tag="${done_c}/${total_c} commits";;
    *)       tag="pending";;
  esac
  printf '  %-18s %s %3d%%  %s' "$name" "$(make_bar "$pct")" "$pct" "$tag"
}

TTY=0; [[ -t 1 ]] && TTY=1
NLINES=$(( 1 + ${#repos[@]} ))
drawn=0
draw() {
  local d=0 run=0 pend=0 f=0 r
  for r in "${repos[@]}"; do case "${STATE[$r]}" in
    done) ((d++));; running) ((run++));; failed) ((f++));; *) ((pend++));; esac; done
  if (( TTY )); then
    (( drawn )) && printf '\033[%dA' "$NLINES"
    printf 'Surveyor scan | %d cores | %d repos: %d done, %d running, %d pending, %d failed\033[K\n' \
      "$JOBS" "${#repos[@]}" "$d" "$run" "$pend" "$f"
    for r in "${repos[@]}"; do printf '%s\033[K\n' "$(render_row "$r")"; done
    drawn=1
  else
    printf '[%s] %d done, %d running, %d pending, %d failed\n' \
      "$(date +%H:%M:%S)" "$d" "$run" "$pend" "$f"
  fi
}

echo "Scanning ${#repos[@]} repos, $JOBS at a time ($SURVEYOR). Logs in $work/logs/"
[[ -n "$EXTRA" ]] && echo "extra scan args: $EXTRA"
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

ok=0; bad=0; badlist=()
for r in "${repos[@]}"; do
  if [[ "${STATE[$r]}" == done ]]; then
    ok=$((ok + 1))
  else
    bad=$((bad + 1)); badlist+=("$(basename "$r")")
  fi
done
echo "done: $ok scanned, $bad failed"
if ((bad)); then echo "failed: ${badlist[*]}  (see $work/logs/)"; exit 1; fi
echo "Next: analyze each with  surveyor analyze $SCAN_DIR/<name>"
