#!/bin/sh
# Runs `evidence-judge hook <adapter> [options]` with the hook JSON on stdin.
#
# With --state-dir, the first hook of a session pins the CLI it found (absolute path and
# checksum) in <state-dir>/pins/<session_id>, keyed by the hook input's session_id. If that CLI
# later goes missing or changes during the session, a stop hook fails closed: it keeps the agent
# from finishing unchecked, once, then stops the session, saying why. A new session pins the CLI
# installed then, so reinstalling or upgrading between sessions blocks nothing. Creating a pin
# prunes session pins older than 30 days and removes the old global <state-dir>/evidence-judge-pin,
# which is used only when the hook input carries no session_id.
# Set EVJ_HOOK_ALLOW_MISSING_JUDGE=1 (in the harness's environment, not the agent's) to allow
# the stop with a notice instead.
# Without the CLI on PATH and no pin, a stop hook allows the stop with a one-line notice and
# record and guard hooks do nothing.
state=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--state-dir" ]; then state=$arg; fi
  prev=$arg
done
sid=""
if [ -n "$state" ]; then
  input=$(mktemp "${TMPDIR:-/tmp}/evidence-judge-hook.XXXXXX" 2>/dev/null)
  if [ -n "$input" ]; then
    cat > "$input"
    exec 0< "$input"
    sid=$(sed -n 's/"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\
\1/p' "$input" | sed -n 2p | tr -cd 'A-Za-z0-9._-' | cut -c 1-128)
    rm -f "$input"
    case "$sid" in
      *[!.]*) ;;
      *) sid="" ;;
    esac
  fi
fi
pin=""
legacy=""
if [ -n "$state" ] && [ -n "$sid" ]; then
  pin="$state/pins/$sid"
  legacy="$state/evidence-judge-pin"
elif [ -n "$state" ]; then
  pin="$state/evidence-judge-pin"
fi
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
  dir=${pin%/*}
  if mkdir -p "$dir" 2>/dev/null; then
    if [ -n "$sid" ]; then
      find "$dir" -type f -mtime +30 -exec rm -f {} + 2>/dev/null
    fi
    if printf '%s\n%s\n' "$bin" "$(cksum < "$bin")" > "$pin.$$" 2>/dev/null \
      && mv -f "$pin.$$" "$pin" 2>/dev/null && [ -n "$legacy" ]; then
      rm -f "$legacy" 2>/dev/null
    fi
    rm -f "$pin.$$" 2>/dev/null
  fi
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
  if [ -n "$sid" ]; then
    fix="restore the CLI, or start a new session to pin the one installed now"
  else
    fix="restore or reinstall the CLI (then delete $pin)"
  fi
  printf 'evidence-judge hook: %s, so this answer cannot be checked. To fix it, %s, or set EVJ_HOOK_ALLOW_MISSING_JUDGE=1 to allow unchecked stops.\n' "$problem" "$fix" >&2
  exit 2
fi
if [ -n "$problem" ]; then
  printf '%s\n' '{"systemMessage":"evidence-judge hook: the pinned evidence-judge CLI is missing or changed, so this answer was not checked."}'
  exit 0
fi
printf '%s\n' '{"systemMessage":"evidence-judge hook: the evidence-judge CLI is not on PATH, so this answer was not checked. Install it with uv tool install (see the Evidence Judge integration guide)."}'
