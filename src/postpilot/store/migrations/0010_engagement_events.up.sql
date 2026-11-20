CREATE TABLE engagement_events (
    id uuid PRIMARY KEY,
    channel text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('comment', 'reaction', 'mention', 'connection', 'other')),
    source_ref text NOT NULL,
    actor text,
    text text,
    post_url text,
    reply_draft text,
    occurred_at timestamptz NOT NULL,
    notified_at timestamptz,
    telegram_message_id bigint,
    created_at timestamptz NOT NULL,
    UNIQUE (channel, source_ref)
);
