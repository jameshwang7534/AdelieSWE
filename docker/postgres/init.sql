-- Runs only when PostgreSQL initializes an empty data volume.
-- The image already includes pgvector; this enables it in POSTGRES_DB.
CREATE EXTENSION IF NOT EXISTS vector;
