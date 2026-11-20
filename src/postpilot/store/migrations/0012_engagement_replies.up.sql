ALTER TABLE engagement_events
    ADD COLUMN reply_status text CHECK (reply_status IN ('SENDING', 'SENT', 'FAILED')),
    ADD COLUMN reply_container_id text,
    ADD COLUMN reply_remote_id text,
    ADD COLUMN reply_sent_at timestamptz,
    ADD COLUMN reply_claimed_at timestamptz;
