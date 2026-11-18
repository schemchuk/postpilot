import os

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow


def connect(
    url: str | None = None, *, schema: str | None = None
) -> psycopg.Connection[TupleRow]:
    conn = psycopg.connect(os.environ["DATABASE_URL"] if url is None else url)
    if schema is not None:
        try:
            with conn.transaction():
                conn.execute(
                    sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
                )
        except BaseException:
            conn.close()
            raise
    return conn
