CREATE TABLE tokens (
    provider text PRIMARY KEY,
    access_token text NOT NULL,
    refresh_token text,
    expires_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
