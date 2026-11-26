LOCK TABLE items, factory_reels IN ACCESS EXCLUSIVE MODE;

CREATE TABLE project_items (
    project text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('reel', 'post')),
    id text NOT NULL,
    tag text NOT NULL CHECK (tag = kind || '/' || id),
    item_id uuid NOT NULL UNIQUE REFERENCES items (id),
    approval text NOT NULL CHECK (approval IN ('auto', 'tap')),
    ai boolean NOT NULL,
    release_at timestamptz NOT NULL,
    post_de text NOT NULL,
    post_threads text,
    threads_skipped boolean NOT NULL CHECK (threads_skipped = (post_threads IS NULL)),
    assets jsonb NOT NULL,
    duration double precision CHECK (kind <> 'reel' OR (duration IS NOT NULL AND duration BETWEEN 3 AND 90)),
    title_de text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project, id)
);

INSERT INTO project_items (
    project, kind, id, tag, item_id, approval, ai, release_at, post_de, post_threads,
    threads_skipped, assets, duration, title_de, created_at, updated_at
)
SELECT 'beekeeper', 'reel', episode, tag, item_id, 'auto', ai, release_at, post_de, post_threads,
    threads_skipped, assets, duration, title_de, created_at, updated_at
FROM factory_reels;

ALTER TABLE items DROP CONSTRAINT items_source_check;
UPDATE items SET source = 'project' WHERE source = 'factory';
ALTER TABLE items ADD CONSTRAINT items_source_check
    CHECK (source IN ('github', 'telegram', 'own_video', 'project'));
