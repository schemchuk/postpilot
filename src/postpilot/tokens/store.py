from dataclasses import dataclass, field
from datetime import datetime

from psycopg import Connection
from psycopg.rows import TupleRow

from postpilot.tokens.base import utc_datetime
from postpilot.tokens.crypto import TokenCipher


@dataclass(frozen=True)
class StoredToken:
    provider: str
    access_token: str = field(repr=False)
    expires_at: datetime
    refresh_token: str | None = field(default=None, repr=False)


class TokenStore:
    def __init__(self, conn: Connection[TupleRow], cipher: TokenCipher) -> None:
        self.conn = conn
        self.cipher = cipher

    def save(
        self,
        provider: str,
        access_token: str,
        expires_at: datetime,
        refresh_token: str | None = None,
    ) -> None:
        expires_at = utc_datetime(expires_at)
        access_ciphertext = self.cipher.encrypt(access_token)
        refresh_ciphertext = (
            self.cipher.encrypt(refresh_token) if refresh_token is not None else None
        )
        with self.conn.transaction():
            self.conn.execute(
                """
                INSERT INTO tokens (provider, access_token, refresh_token, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (provider) DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = now()
                """,
                (provider, access_ciphertext, refresh_ciphertext, expires_at),
            )

    def get(self, provider: str) -> StoredToken | None:
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT access_token, refresh_token, expires_at FROM tokens WHERE provider = %s",
                (provider,),
            ).fetchone()
        if row is None:
            return None
        access_ciphertext, refresh_ciphertext, expires_at = row
        return StoredToken(
            provider,
            self.cipher.decrypt(access_ciphertext),
            expires_at,
            self.cipher.decrypt(refresh_ciphertext)
            if refresh_ciphertext is not None
            else None,
        )

    def delete(self, provider: str) -> None:
        with self.conn.transaction():
            self.conn.execute("DELETE FROM tokens WHERE provider = %s", (provider,))
