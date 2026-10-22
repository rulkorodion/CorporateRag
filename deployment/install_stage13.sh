#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${CORPORATE_RAG_APP_ROOT:-/opt/corporate-rag/app}"
bash "$APP_ROOT/deployment/install_stage12.sh"
install -m 0755 \
  "$APP_ROOT/deployment/scripts/corporate-rag-performance-profile" \
  /usr/local/sbin/

echo "Stage 13 performance profile tool installed"
