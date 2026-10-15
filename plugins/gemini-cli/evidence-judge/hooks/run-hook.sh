#!/bin/sh
# Runs `evidence-judge hook <adapter> [options]` with the hook JSON on stdin.
# Without the CLI on PATH, a stop hook allows the stop with a one-line notice
# and a record hook does nothing.
if command -v evidence-judge >/dev/null 2>&1; then
  exec evidence-judge hook "$@"
fi
case "$1" in
  gemini-record) printf '%s\n' '{}' ;;
  *-record) ;;
  *) printf '%s\n' '{"systemMessage":"evidence-judge hook: the evidence-judge CLI is not on PATH, so this answer was not checked. Install it with uv tool install (see the Evidence Judge integration guide)."}' ;;
esac
