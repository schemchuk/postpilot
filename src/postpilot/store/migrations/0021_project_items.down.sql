LOCK TABLE project_items IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM project_items WHERE project <> 'beekeeper' OR kind <> 'reel') THEN
        RAISE EXCEPTION 'Cannot roll back project_items: rows exist outside beekeeper reels';
    END IF;
    IF EXISTS (SELECT 1 FROM project_items WHERE approval <> 'auto') THEN
        RAISE EXCEPTION 'Cannot roll back project_items: factory reels require auto approval';
    END IF;
END;
$$;

INSERT INTO factory_reels (
    episode, tag, item_id, ai, release_at, post_de, post_threads, threads_skipped,
    assets, duration, title_de, created_at, updated_at
)
SELECT id, tag, item_id, ai, release_at, post_de, post_threads, threads_skipped,
    assets, duration, title_de, created_at, updated_at
FROM project_items
ON CONFLICT (episode) DO UPDATE SET
    tag = EXCLUDED.tag,
    item_id = EXCLUDED.item_id,
    ai = EXCLUDED.ai,
    release_at = EXCLUDED.release_at,
    post_de = EXCLUDED.post_de,
    post_threads = EXCLUDED.post_threads,
    threads_skipped = EXCLUDED.threads_skipped,
    assets = EXCLUDED.assets,
    duration = EXCLUDED.duration,
    title_de = EXCLUDED.title_de,
    created_at = EXCLUDED.created_at,
    updated_at = EXCLUDED.updated_at;

ALTER TABLE items DROP CONSTRAINT items_source_check;
UPDATE items SET source = 'factory' WHERE source = 'project';
ALTER TABLE items ADD CONSTRAINT items_source_check
    CHECK (source IN ('github', 'telegram', 'own_video', 'factory'));
DROP TABLE project_items;
