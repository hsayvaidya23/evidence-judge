#!/bin/sh
# Runs `evidence-judge hook <adapter> [options]` with the hook JSON on stdin.
#
# With --state-dir, the first run pins the CLI it found (absolute path and checksum) in
# <state-dir>/evidence-judge-pin. If that file later goes missing or changes, a stop hook fails
# closed: it keeps the agent from finishing unchecked, once, then stops the session, saying why.
# Set EVJ_HOOK_ALLOW_MISSING_JUDGE=1 (in the harness's environment, not the agent's) to allow
# the stop with a notice instead; delete the pin file after reinstalling the CLI elsewhere.
# Without the CLI on PATH and no pin, a stop hook allows the stop with a one-line notice and
# record and guard hooks do nothing.
state=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--state-dir" ]; then state=$arg; fi
  prev=$arg
done
pin=""
if [ -n "$state" ]; then pin="$state/evidence-judge-pin"; fi
bin=$(command -v evidence-judge 2>/dev/null)
problem=""
if [ -n "$pin" ] && [ -f "$pin" ]; then
  pinned=$(sed -n 1p "$pin")
  sum=$(sed -n 2p "$pin")
  if [ ! -x "$pinned" ]; then
    problem="the evidence-judge CLI pinned at $pinned is gone"
  elif [ "$(cksum < "$pinned")" != "$sum" ]; then
    problem="the evidence-judge CLI at $pinned changed since it was pinned"
  else
    bin=$pinned
  fi
elif [ -n "$pin" ] && [ -n "$bin" ] && [ -x "$bin" ]; then
  mkdir -p "$state" 2>/dev/null && printf '%s\n%s\n' "$bin" "$(cksum < "$bin")" > "$pin" 2>/dev/null
fi
if [ -z "$problem" ] && [ -n "$bin" ]; then
  exec "$bin" hook "$@"
fi
case "$1" in
  gemini-record | gemini-guard) printf '%s\n' '{}' ; exit 0 ;;
  *-record | *-guard) exit 0 ;;
esac
if [ -n "$problem" ] && [ "${EVJ_HOOK_ALLOW_MISSING_JUDGE:-}" != "1" ]; then
  if cat | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
    printf '%s\n' '{"continue":false,"stopReason":"evidence-judge hook: the pinned evidence-judge CLI is missing or changed, so the answer was not checked."}'
    exit 0
  fi
  printf 'evidence-judge hook: %s, so this answer cannot be checked. Restore or reinstall the CLI (then delete %s), or set EVJ_HOOK_ALLOW_MISSING_JUDGE=1 to allow unchecked stops.\n' "$problem" "$pin" >&2
  exit 2
fi
if [ -n "$problem" ]; then
  printf '%s\n' '{"systemMessage":"evidence-judge hook: the pinned evidence-judge CLI is missing or changed, so this answer was not checked."}'
  exit 0
fi
printf '%s\n' '{"systemMessage":"evidence-judge hook: the evidence-judge CLI is not on PATH, so this answer was not checked. Install it with uv tool install (see the Evidence Judge integration guide)."}'
