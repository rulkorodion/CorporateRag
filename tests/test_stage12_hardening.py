from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deployment"


def read(relative_path: str) -> str:
    return (DEPLOYMENT / relative_path).read_text(encoding="utf-8")


def test_compose_has_bounded_logs_and_container_hardening() -> None:
    compose = read("compose.production.yml")

    assert 'driver: local' in compose
    assert 'max-size: "20m"' in compose
    assert 'max-file: "5"' in compose
    assert compose.count('cap_drop: ["ALL"]') == 5
    assert compose.count("no-new-privileges:true") == 5
    assert compose.count("read_only: true") == 5
    assert "privileged: true" not in compose
    assert "127.0.0.1" in compose
    assert "/opt/models/Qwen3-Embedding-0.6B:/models/embedding:ro" in compose
    assert "/opt/corporate-rag/knowledge-base:/app/knowledge-base:ro" in compose


def test_hourly_incremental_timer_is_persistent_and_serialized() -> None:
    timer = read("systemd/corporate-rag-update.timer")
    wrapper = read("scripts/corporate-rag-update")

    assert "OnCalendar=hourly" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=5min" in timer
    assert "flock -n 9" in wrapper
    assert "corporate-rag-maintenance.lock" in wrapper
    assert "INCREMENTAL_REPORT_RETENTION" in wrapper


def test_backup_uses_qdrant_snapshot_checksums_and_retention() -> None:
    backup = read("scripts/corporate-rag-backup")
    restore = read("scripts/corporate-rag-restore")

    assert "/snapshots?wait=true" in backup
    assert "qdrant.snapshot" in backup
    assert "sqlite3" in backup
    assert "sha256sum" in backup
    assert "BACKUP_RETENTION:-7" in backup
    assert "priority=snapshot" in restore
    assert "_restore_test_" in restore
    assert "--confirm-production" in restore
    assert "sha256sum -c" in restore


def test_autostart_waits_for_dependencies_and_readiness() -> None:
    service = read("systemd/corporate-rag.service")
    start = read("scripts/corporate-rag-start")

    assert "Requires=docker.service" in service
    assert "WantedBy=multi-user.target" in service
    assert "http://127.0.0.1:6333/" in start
    assert "http://127.0.0.1:11434/api/tags" in start
    assert "http://127.0.0.1:8010/ready" in start
    assert "http://127.0.0.1:3001/api/ready" in start
    assert "Restart=on-failure" in service
    assert "RestartSec=30s" in service
    assert "StartLimitIntervalSec=0" in service


def test_editor_uploads_use_separate_queue_and_systemd_path() -> None:
    compose = read("compose.production.yml")
    path_unit = read("systemd/corporate-rag-upload.path")
    service = read("systemd/corporate-rag-upload.service")
    promoter = read("scripts/corporate-rag-promote-uploads")

    assert "/opt/corporate-rag/admin-upload:/app/admin-upload:rw" in compose
    assert "/opt/corporate-rag/knowledge-base:/app/knowledge-base:ro" in compose
    assert "PathExistsGlob=/opt/corporate-rag/admin-upload/queue/*/request.json" in path_unit
    assert "corporate-rag-upload.service" in path_unit
    assert "corporate-rag-promote-uploads" in service
    assert "CORPORATE_RAG_LOCK_HELD" in promoter
    assert "payload checksum mismatch" in promoter
    assert "os.replace(payload, destination)" in promoter


def test_editor_credentials_are_external_and_password_is_derived() -> None:
    compose = read("compose.production.yml")
    credentials = read("scripts/corporate-rag-admin-credentials")

    assert "/opt/corporate-rag/config/admin.env" in compose
    assert "ADMIN_PASSWORD_HASH" not in compose
    assert "hashlib.pbkdf2_hmac" in credentials
    assert "ADMIN_SESSION_SECRET" in credentials
    assert "os.chmod(temporary, 0o600)" in credentials
