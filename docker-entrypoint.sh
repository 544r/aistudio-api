#!/bin/sh
set -e

# 容器里创建的所有文件默认仅属主可读写：auth.json / profile / 运行配置
# 都含 Google 会话凭据或 API Key，不能按默认 022 umask 落成 0644。
umask 077

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
