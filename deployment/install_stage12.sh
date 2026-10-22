#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deployment/install_stage12.sh" >&2
  exit 1
fi

APP_ROOT="${CORPORATE_RAG_APP_ROOT:-/opt/corporate-rag/app}"

install -d -m 0700 /opt/corporate-rag/backups/production
install -d -o 1001 -g 1001 -m 0750 \
  /opt/corporate-rag/admin-upload \
  /opt/corporate-rag/admin-upload/queue \
  /opt/corporate-rag/admin-upload/jobs \
  /opt/corporate-rag/admin-upload/trigger \
  /opt/corporate-rag/admin-upload/runtime
install -m 0755 "$APP_ROOT/deployment/scripts/corporate-rag-update" /usr/local/sbin/
install -m 0755 "$APP_ROOT/deployment/scripts/corporate-rag-promote-uploads" /usr/local/sbin/
install -m 0755 "$APP_ROOT/deployment/scripts/corporate-rag-admin-credentials" /usr/local/sbin/

if [[ ! -s /opt/corporate-rag/config/admin.env ]]; then
  /usr/local/sbin/corporate-rag-admin-credentials editor
fi
install -m 0755 "$APP_ROOT/deployment/scripts/corporate-rag-start" /usr/local/sbin/
install -m 0755 "$APP_ROOT/deployment/scripts/corporate-rag-backup" /usr/local/sbin/
install -m 0755 "$APP_ROOT/deployment/scripts/corporate-rag-restore" /usr/local/sbin/

install -m 0644 "$APP_ROOT/deployment/systemd/corporate-rag.service" /etc/systemd/system/
install -m 0644 "$APP_ROOT/deployment/systemd/corporate-rag-update.service" /etc/systemd/system/
install -m 0644 "$APP_ROOT/deployment/systemd/corporate-rag-update.timer" /etc/systemd/system/
install -m 0644 "$APP_ROOT/deployment/systemd/corporate-rag-backup.service" /etc/systemd/system/
install -m 0644 "$APP_ROOT/deployment/systemd/corporate-rag-backup.timer" /etc/systemd/system/
install -m 0644 "$APP_ROOT/deployment/systemd/corporate-rag-upload.service" /etc/systemd/system/
install -m 0644 "$APP_ROOT/deployment/systemd/corporate-rag-upload.path" /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now corporate-rag.service
systemctl enable --now corporate-rag-update.timer
systemctl enable --now corporate-rag-backup.timer
systemctl enable --now corporate-rag-upload.path

echo "Corporate RAG production units installed"
systemctl list-timers --all 'corporate-rag-*'
