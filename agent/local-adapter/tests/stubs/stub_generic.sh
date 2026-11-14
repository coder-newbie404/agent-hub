#!/usr/bin/env bash
# Minimal generic JSONL agent for base-runner tests (STUB_* contract).
if [ -n "${STUB_ENV_FILE:-}" ]; then env > "$STUB_ENV_FILE"; fi
if [ -n "${STUB_ARGS_FILE:-}" ]; then printf '%s\n' "$@" > "$STUB_ARGS_FILE"; fi
echo '{"type":"session","id":"stub-generic-1"}'
if [ "${STUB_FAIL:-}" = "1" ]; then
  echo '{"type":"error","message":"driver says boom"}'
  echo "stub exploded" >&2
  exit 1
fi
if [ -n "${STUB_DELAY:-}" ]; then sleep "${STUB_DELAY}"; fi
echo '{"type":"delta","text":"stub "}'
echo '{"type":"delta","text":"reply"}'
echo '{"type":"reply","text":"stub reply"}'
