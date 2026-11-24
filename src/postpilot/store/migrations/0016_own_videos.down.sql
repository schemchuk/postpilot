ALTER TABLE items DROP CONSTRAINT items_source_check;
ALTER TABLE items ADD CONSTRAINT items_source_check
    CHECK (source IN ('github', 'telegram'));

DROP TABLE own_video_updates;
DROP TABLE own_videos;
