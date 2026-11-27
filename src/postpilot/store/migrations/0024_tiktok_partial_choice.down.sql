DELETE FROM tiktok_settings WHERE privacy_level IS NULL;
ALTER TABLE tiktok_settings ALTER COLUMN privacy_level SET NOT NULL;
DELETE FROM publications WHERE status = 'SKIPPED';
ALTER TABLE publications DROP CONSTRAINT publications_status_check;
ALTER TABLE publications ADD CONSTRAINT publications_status_check
    CHECK (status IN ('PUBLISHING', 'PUBLISHED', 'FAILED'));
DELETE FROM video_publications WHERE status = 'SKIPPED';
ALTER TABLE video_publications DROP CONSTRAINT video_publications_status_check;
ALTER TABLE video_publications ADD CONSTRAINT video_publications_status_check
    CHECK (status IN ('PUBLISHING', 'PUBLISHED', 'FAILED'));
