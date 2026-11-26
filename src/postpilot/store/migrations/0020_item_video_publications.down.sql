DROP VIEW item_publications;
INSERT INTO publications
    (item_id, channel, status, remote_id, error, updated_at, remote_ref, remote_ref_claimed_at)
SELECT item_id, channel, status, remote_id, error, updated_at, remote_ref, remote_ref_claimed_at
FROM video_publications WHERE item_id IS NOT NULL;
DELETE FROM video_publications WHERE item_id IS NOT NULL;
ALTER TABLE video_publications
    DROP COLUMN item_id,
    DROP CONSTRAINT video_publications_render_id_channel_key,
    ADD PRIMARY KEY (render_id, channel);
ALTER TABLE factory_reels DROP COLUMN title_de;
