CREATE TABLE telegram_inbox (
    update_id bigint PRIMARY KEY,
    received_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL,
    processed_at timestamptz,
    attempts integer NOT NULL DEFAULT 0
);

CREATE INDEX telegram_inbox_pending_idx ON telegram_inbox (update_id)
WHERE processed_at IS NULL;
