#!/bin/bash
# AI Knockout — Database Restore Script
# WARNING: This will REPLACE the target database contents
set -euo pipefail

BACKUP_FILE="${1:?Usage: restore.sh <backup_file.sql.gz>}"
TARGET_DB="${2:-${POSTGRES_DB:-aik_db}}"

if [ ! -f "${BACKUP_FILE}" ]; then
    echo "[RESTORE] ERROR: Backup file not found: ${BACKUP_FILE}"
    exit 1
fi

echo "[RESTORE] Restoring ${BACKUP_FILE} into database: ${TARGET_DB}"
echo "[RESTORE] WARNING: This will replace existing data in ${TARGET_DB}"
read -p "Continue? (y/N) " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "[RESTORE] Aborted."
    exit 0
fi

gunzip -c "${BACKUP_FILE}" | docker compose exec -T postgres psql \
    -U "${POSTGRES_USER:-aik_user}" \
    -d "${TARGET_DB}" \
    --set ON_ERROR_STOP=on

echo "[RESTORE] Success: Database ${TARGET_DB} restored from ${BACKUP_FILE}"
