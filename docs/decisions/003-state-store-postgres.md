# 003. State store: free managed Postgres

Status: PROPOSED (provider chosen in task 2 after checking current free-tier limits)

Context: Actions runners are ephemeral; state (items, publications, offsets, series, budget) must
persist and support transactional claiming for idempotency.
Options: Postgres on a free tier (Neon or Supabase candidates), SQLite committed to the repo (race and
history noise), SQLite as a cached artifact (can be lost).
Decision: Postgres, accessed only through `store/`, with SQL migrations in the repo.
Consequences: one more secret (`DATABASE_URL`); provider is replaceable since only standard SQL is used.
