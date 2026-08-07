#!/usr/bin/env bash
# PreToolUse(Bash) guard: refuse `python -m unittest` unless it is `.venv/bin/python`.
#
# Why this is a hook and not a line in CLAUDE.md: bare `python -m unittest` in this
# repo discovers **zero** tests and prints OK. It does not error, so an agent that
# runs it gets a green light on an untested change. The system interpreter also has
# none of the deps, so the failure mode is either a silent pass or a dozen import
# errors — never an honest test result.
#
# Reads the hook payload on stdin, writes a PreToolUse decision on stdout.
set -uo pipefail

payload="$(cat)"
command_text="$(printf '%s' "$payload" | jq -r '.tool_input.command // ""')"

# Every token that sits immediately before `-m unittest`, i.e. the interpreter.
interpreters="$(printf '%s' "$command_text" | grep -oP '\S+(?=\s+-m\s+unittest\b)' || true)"

[ -z "$interpreters" ] && exit 0

while IFS= read -r interp; do
  [ -z "$interp" ] && continue
  case "$interp" in
    *.venv/bin/python|*.venv/bin/python3) continue ;;
  esac

  jq -nc --arg interp "$interp" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: (
        "Blocked: `" + $interp + " -m unittest` does not run the tests in this repo. " +
        "Bare `python -m unittest` discovers 0 tests here and still prints OK, and the " +
        "system interpreter is missing the deps. Use the pinned command instead:\n\n" +
        "    .venv/bin/python -m unittest discover -s tests\n\n" +
        "(or `make test`, which is the same command). Expect 1100+ tests in under a minute."
      )
    }
  }'
  exit 0
done <<< "$interpreters"

exit 0
