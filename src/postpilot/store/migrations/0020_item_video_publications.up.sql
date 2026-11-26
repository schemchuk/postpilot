ALTER TABLE factory_reels ADD COLUMN title_de text;

ALTER TABLE video_publications DROP CONSTRAINT video_publications_pkey;
ALTER TABLE video_publications ALTER COLUMN render_id DROP NOT NULL;
ALTER TABLE video_publications ADD COLUMN item_id uuid REFERENCES items(id);
ALTER TABLE video_publications
    ADD CHECK ((render_id IS NULL) <> (item_id IS NULL)),
    ADD UNIQUE (render_id, channel),
    ADD UNIQUE (item_id, channel);

INSERT INTO video_publications
    (item_id, channel, status, remote_id, error, updated_at, remote_ref, remote_ref_claimed_at)
SELECT item_id, channel, status, remote_id, error, updated_at, remote_ref, remote_ref_claimed_at
FROM publications WHERE channel = 'youtube';
DELETE FROM publications WHERE channel = 'youtube';

CREATE VIEW item_publications AS
    SELECT item_id, channel, status, remote_id, error, updated_at, remote_ref, remote_ref_claimed_at
    FROM publications
    UNION ALL
    SELECT item_id, channel, status, remote_id, error, updated_at, remote_ref, remote_ref_claimed_at
    FROM video_publications WHERE item_id IS NOT NULL;
