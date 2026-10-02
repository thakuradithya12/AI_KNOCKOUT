-- AI Knockout PostgreSQL initialization
-- Runs only on first database creation (empty data volume)

-- Ensure UTC timezone
ALTER DATABASE aik_db SET timezone TO 'UTC';

-- Extensions for future phases
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
