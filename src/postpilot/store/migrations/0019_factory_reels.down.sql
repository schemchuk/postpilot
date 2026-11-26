ALTER TABLE items DROP CONSTRAINT items_source_check;
ALTER TABLE items ADD CONSTRAINT items_source_check
    CHECK (source IN ('github', 'telegram', 'own_video'));

DROP TABLE factory_reels;
