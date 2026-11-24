ALTER TABLE items DROP CONSTRAINT items_source_check;
ALTER TABLE items ADD CONSTRAINT items_source_check
    CHECK (source IN ('github', 'telegram', 'own_video'));

CREATE TABLE own_videos (
    id uuid PRIMARY KEY,
    item_id uuid UNIQUE REFERENCES items (id),
    chat_id bigint NOT NULL,
    prompt_message_id bigint,
    file_id text NOT NULL,
    file_unique_id text NOT NULL UNIQUE,
    size_bytes bigint,
    duration int,
    width int,
    height int,
    ai_generated boolean NOT NULL,
    interview text NOT NULL DEFAULT '',
    status text NOT NULL CHECK (status IN ('collecting', 'generated', 'skipped')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chat_id, prompt_message_id)
);

CREATE TABLE own_video_updates (update_id bigint PRIMARY KEY);
