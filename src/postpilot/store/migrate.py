from pathlib import Path

from psycopg import Connection
from psycopg.rows import TupleRow

MIGRATIONS = Path(__file__).with_name("migrations")


def _lock_migrations(conn: Connection[TupleRow]) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), 0)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
    """)


def up(conn: Connection[TupleRow]) -> None:
    for migration in sorted(MIGRATIONS.glob("*.up.sql")):
        version = migration.name.removesuffix(".up.sql")
        with conn.transaction():
            _lock_migrations(conn)
            if (
                conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)
                ).fetchone()
                is not None
            ):
                continue
            conn.execute(migration.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
            )


def down(conn: Connection[TupleRow], steps: int = 1) -> None:
    for _ in range(steps):
        with conn.transaction():
            _lock_migrations(conn)
            row = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return
            (version,) = row
            migration = MIGRATIONS / f"{version}.down.sql"
            conn.execute(migration.read_text(encoding="utf-8"))
            conn.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
