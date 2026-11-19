CREATE TABLE video_publications (
    render_id uuid NOT NULL REFERENCES video_renders(id),
    channel text NOT NULL,
    status text NOT NULL CHECK (status IN ('PUBLISHING', 'PUBLISHED', 'FAILED')),
    remote_id text,
    remote_ref text,
    remote_ref_claimed_at timestamptz,
    error text,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (render_id, channel)
);
