#!/bin/bash
# AI Knockout — Backup/Restore Test Script
# Tests backup and restore using a disposable test database
set -euo pipefail

TEST_DB="aik_backup_test"
BACKUP_DIR="./backups/test"
PG_USER="${POSTGRES_USER:-aik_user}"
PG_DB="${POSTGRES_DB:-aik_db}"

echo "=== AI Knockout Backup/Restore Test ==="
echo

# Step 1: Create test data
echo "[TEST] Inserting test data into ${PG_DB}..."
docker compose exec -T postgres psql -U "${PG_USER}" -d "${PG_DB}" <<'SQL'
CREATE TABLE IF NOT EXISTS _backup_test (id serial PRIMARY KEY, value text, created_at timestamptz DEFAULT now());
INSERT INTO _backup_test (value) VALUES ('backup_test_marker_' || gen_random_uuid()::text);
SQL

TEST_VALUE=$(docker compose exec -T postgres psql -U "${PG_USER}" -d "${PG_DB}" -t -c "SELECT value FROM _backup_test ORDER BY id DESC LIMIT 1;" | tr -d '[:space:]')
echo "[TEST] Test marker: ${TEST_VALUE}"

# Step 2: Backup
mkdir -p "${BACKUP_DIR}"
TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S_UTC")
BACKUP_FILE="${BACKUP_DIR}/test_backup_${TIMESTAMP}.sql.gz"

echo "[TEST] Creating backup..."
docker compose exec -T postgres pg_dump \
    -U "${PG_USER}" -d "${PG_DB}" \
    --clean --if-exists --no-owner \
    | gzip > "${BACKUP_FILE}"

if [ ! -s "${BACKUP_FILE}" ]; then
    echo "[TEST] FAIL: Backup file is empty"
    exit 1
fi
echo "[TEST] Backup created: ${BACKUP_FILE}"

# Step 3: Create disposable test database
echo "[TEST] Creating disposable test database: ${TEST_DB}"
docker compose exec -T postgres psql -U "${PG_USER}" -d postgres -c "DROP DATABASE IF EXISTS ${TEST_DB};"
docker compose exec -T postgres psql -U "${PG_USER}" -d postgres -c "CREATE DATABASE ${TEST_DB};"

# Step 4: Restore into test database
echo "[TEST] Restoring backup into ${TEST_DB}..."
gunzip -c "${BACKUP_FILE}" | docker compose exec -T postgres psql -U "${PG_USER}" -d "${TEST_DB}" --set ON_ERROR_STOP=on 2>&1 || true

# Step 5: Verify
echo "[TEST] Verifying restored data..."
RESTORED_VALUE=$(docker compose exec -T postgres psql -U "${PG_USER}" -d "${TEST_DB}" -t -c "SELECT value FROM _backup_test ORDER BY id DESC LIMIT 1;" 2>/dev/null | tr -d '[:space:]')

if [ "${RESTORED_VALUE}" = "${TEST_VALUE}" ]; then
    echo "[TEST] PASS: Backup/restore verified — test marker matches"
else
    echo "[TEST] FAIL: Restored value '${RESTORED_VALUE}' does not match original '${TEST_VALUE}'"
    docker compose exec -T postgres psql -U "${PG_USER}" -d postgres -c "DROP DATABASE IF EXISTS ${TEST_DB};"
    exit 1
fi

# Step 6: Cleanup
echo "[TEST] Cleaning up..."
docker compose exec -T postgres psql -U "${PG_USER}" -d postgres -c "DROP DATABASE IF EXISTS ${TEST_DB};"
docker compose exec -T postgres psql -U "${PG_USER}" -d "${PG_DB}" -c "DROP TABLE IF EXISTS _backup_test;"
rm -rf "${BACKUP_DIR}"

echo "[TEST] Backup/restore test PASSED"
echo "=== Test Complete ==="
