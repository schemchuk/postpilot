# 003. State store: free managed Postgres

Status: ACCEPTED (Volodymyr, 2026-11-18) - T02 chose Neon (free plan: 1 GB per project, compute suspends after 5 minutes and wakes on connect).

Context: Actions runners are ephemeral; state (items, publications, offsets, series, budget) must
persist and support transactional claiming for idempotency.
Options: Postgres on a free tier (Neon or Supabase candidates), SQLite committed to the repo (race and
history noise), SQLite as a cached artifact (can be lost).
Supabase was rejected because free projects pause after one week of inactivity and require manual restoration.
Decision: Postgres, accessed only through `store/`, with SQL migrations in the repo.
Consequences: one more secret (`DATABASE_URL`); provider is replaceable since only standard SQL is used.
