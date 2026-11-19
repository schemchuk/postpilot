CREATE TABLE video_renders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id uuid NOT NULL REFERENCES items (id),
    lang text NOT NULL,
    heygen_video_id text,
    status text NOT NULL CHECK (status IN ('RENDERING', 'READY', 'APPROVED', 'REJECTED', 'FAILED')),
    script text,
    -- Durable across ephemeral runners. Retaining MP4s increases database storage.
    video bytea,
    duration real,
    requested_at timestamptz NOT NULL DEFAULT now(),
    approved_by bigint,
    approved_at timestamptz,
    retried boolean NOT NULL DEFAULT false,
    UNIQUE (item_id, lang)
);
