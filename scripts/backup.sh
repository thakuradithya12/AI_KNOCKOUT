#!/bin/bash
# AI Knockout — Database Backup Script
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S_UTC")
BACKUP_FILE="${BACKUP_DIR}/aik_backup_${TIMESTAMP}.sql.gz"

mkdir -p "${BACKUP_DIR}"

echo "[BACKUP] Starting database backup at ${TIMESTAMP}"

docker compose exec -T postgres pg_dump \
    -U "${POSTGRES_USER:-aik_user}" \
    -d "${POSTGRES_DB:-aik_db}" \
    --clean \
    --if-exists \
    --no-owner \
    | gzip > "${BACKUP_FILE}"

if [ -s "${BACKUP_FILE}" ]; then
    SIZE=$(du -h "${BACKUP_FILE}" | cut -f1)
    echo "[BACKUP] Success: ${BACKUP_FILE} (${SIZE})"
else
    echo "[BACKUP] FAILED: Backup file is empty or pg_dump failed"
    rm -f "${BACKUP_FILE}"
    exit 1
fi
