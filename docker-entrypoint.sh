#!/bin/sh
set -e

CONFIG_FILE="${AISTUDIO_CONFIG_FILE:-/app/data/config.yaml}"
TEMPLATE_FILE="/app/defaults/config.yaml"
FALLBACK_TEMPLATE="/app/config.yaml"

if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f "$TEMPLATE_FILE" ]; then
        mkdir -p "$(dirname "$CONFIG_FILE")"
        cp "$TEMPLATE_FILE" "$CONFIG_FILE"
        echo "[entrypoint] Initialized default config.yaml at $CONFIG_FILE from $TEMPLATE_FILE"
    elif [ -f "$FALLBACK_TEMPLATE" ]; then
        mkdir -p "$(dirname "$CONFIG_FILE")"
        cp "$FALLBACK_TEMPLATE" "$CONFIG_FILE"
        echo "[entrypoint] Initialized default config.yaml at $CONFIG_FILE from $FALLBACK_TEMPLATE"
    fi
fi

exec "$@"
