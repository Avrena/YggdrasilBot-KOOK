-- Enable required extensions on first Postgres database initialization.
-- pg_search depends on vector.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_search;
