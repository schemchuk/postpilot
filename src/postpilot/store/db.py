import os
import re

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import TupleRow


def connect(
    url: str | None = None, *, schema: str | None = None
) -> psycopg.Connection[TupleRow]:
    if url is None:
        url = os.environ["DATABASE_URL"]
    if schema is None:
        return psycopg.connect(url)

    host = conninfo_to_dict(url).get("host")
    if isinstance(host, str) and "-pooler" in host.lower():
        raise ValueError("Schema-scoped connections need a direct, non-pooler host")
    if re.fullmatch(r"[a-z_][a-z0-9_]*", schema) is None:
        raise ValueError("Invalid schema name")

    return psycopg.connect(url, options=f"-c search_path={schema}")
