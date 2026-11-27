CREATE TABLE factory_reels (
    episode text PRIMARY KEY,
    tag text NOT NULL CHECK (tag = 'reel/' || episode),
    item_id uuid NOT NULL UNIQUE REFERENCES items (id),
    ai boolean NOT NULL,
    release_at timestamptz NOT NULL,
    post_de text NOT NULL,
    post_threads text,
    threads_skipped boolean NOT NULL CHECK (threads_skipped = (post_threads IS NULL)),
    assets jsonb NOT NULL,
    duration double precision NOT NULL CHECK (duration BETWEEN 3 AND 90),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    title_de text
);
