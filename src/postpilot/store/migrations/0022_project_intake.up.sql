ALTER TABLE project_items
    ADD COLUMN channels text[],
    ADD COLUMN fingerprint text,
    ADD COLUMN preview_message_id bigint;

CREATE TABLE project_rejections (
    project text NOT NULL,
    tag text NOT NULL,
    fingerprint text NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project, tag, fingerprint)
);
