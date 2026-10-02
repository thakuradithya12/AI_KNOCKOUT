# AI Knockout — Database Backup & Restore Guide

This document describes the backup and restoration procedures for the PostgreSQL database underpinning AI Knockout Phase 1.

---

## 1. Overview

- **Database Engine**: PostgreSQL 16 Alpine
- **Storage**: Docker named volume `aik-pgdata`
- **Backup Utility**: `pg_dump` executed inside the container
- **Compression**: Gzip (`.sql.gz`)
- **Scripts Directory**: `scripts/`

---

## 2. Performing a Backup

The backup script `scripts/backup.sh` connects to the running `aik-postgres` container, executes `pg_dump`, compresses the output, and writes a timestamped archive to `./backups/`.

### Command:
```bash
bash scripts/backup.sh
```

### Parameters:
- `BACKUP_DIR`: Target directory (defaults to `./backups`).
- `POSTGRES_USER`: Database user (defaults to `aik_user`).
- `POSTGRES_DB`: Target database (defaults to `aik_db`).

### Example Output:
```text
[BACKUP] Starting database backup at 20260929_020000_UTC
[BACKUP] Success: ./backups/aik_backup_20260929_020000_UTC.sql.gz (14K)
```

---

## 3. Restoring from a Backup

The restore script `scripts/restore.sh` extracts a `.sql.gz` archive and pipes it into `psql` within the target container.

### Command:
```bash
bash scripts/restore.sh ./backups/aik_backup_20260929_020000_UTC.sql.gz
```

> [!WARNING]
> Restoring a database replaces existing schema and records. The script requires interactive confirmation (`y/N`) before proceeding.

---

## 4. Automated Backup Verification Test

To verify that backups are both created and cleanly restorable without risking live data, use `scripts/backup_test.sh`:

```bash
bash scripts/backup_test.sh
```

### Test Workflow:
1. Writes a random test marker record into `_backup_test` in the active database.
2. Creates a compressed backup archive.
3. Spawns an isolated temporary database (`aik_backup_test`).
4. Restores the backup archive into the temporary database.
5. Verifies that the test marker in the restored database matches the source.
6. Cleans up the temporary database and test table.
7. Reports `PASS` or `FAIL`.
