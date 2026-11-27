ALTER TABLE tiktok_settings ALTER COLUMN privacy_level DROP NOT NULL;
ALTER TABLE publications DROP CONSTRAINT publications_status_check;
ALTER TABLE publications ADD CONSTRAINT publications_status_check
    CHECK (status IN ('PUBLISHING', 'PUBLISHED', 'FAILED', 'SKIPPED'));
ALTER TABLE video_publications DROP CONSTRAINT video_publications_status_check;
ALTER TABLE video_publications ADD CONSTRAINT video_publications_status_check
    CHECK (status IN ('PUBLISHING', 'PUBLISHED', 'FAILED', 'SKIPPED'));
