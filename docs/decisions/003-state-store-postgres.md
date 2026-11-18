# 003. State store: free managed Postgres

Status: ACCEPTED (Volodymyr, 2026-11-18) - Postgres provider is chosen in task 2

Context: Actions runners are ephemeral; state (items, publications, offsets, series, budget) must
persist and support transactional claiming for idempotency.
Options: Postgres on a free tier (Neon or Supabase candidates), SQLite committed to the repo (race and
history noise), SQLite as a cached artifact (can be lost).
Decision: Postgres, accessed only through `store/`, with SQL migrations in the repo.
Consequences: one more secret (`DATABASE_URL`); provider is replaceable since only standard SQL is used.
