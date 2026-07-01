#!/usr/bin/env bash
# Resolve runtime directories from .env (source after ROOT is set and .env is loaded).
: "${ROOT:?ROOT must be set before sourcing runtime_paths.sh}"

_resolve_runtime_path() {
  local value="$1"
  if [[ "$value" == /* ]]; then
    printf '%s' "$value"
  else
    printf '%s/%s' "$ROOT" "$value"
  fi
}

RUNTIME_DIR="${RUNTIME_DIR:-tmp}"
LOGS_DIR="${LOGS_DIR:-${RUNTIME_DIR}/logs}"
RUN_DIR="${RUN_DIR:-${RUNTIME_DIR}/run}"
PRICE_LOG_DIR="${PRICE_LOG_DIR:-${RUNTIME_DIR}/data/price_logs}"

RUNTIME_ROOT="$(_resolve_runtime_path "$RUNTIME_DIR")"
LOGS_DIR="$(_resolve_runtime_path "$LOGS_DIR")"
RUN_DIR="$(_resolve_runtime_path "$RUN_DIR")"
PRICE_LOG_DIR="$(_resolve_runtime_path "$PRICE_LOG_DIR")"

# Legacy aliases used by start/stop scripts
LOG_DIR="$LOGS_DIR"
PID_DIR="$RUN_DIR"

mkdir -p "$LOGS_DIR" "$RUN_DIR" "$PRICE_LOG_DIR"

export RUNTIME_DIR LOGS_DIR RUN_DIR PRICE_LOG_DIR RUNTIME_ROOT LOG_DIR PID_DIR
