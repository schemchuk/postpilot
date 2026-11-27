DROP TABLE project_rejections;
ALTER TABLE project_items
    DROP COLUMN preview_message_id,
    DROP COLUMN fingerprint,
    DROP COLUMN channels;
