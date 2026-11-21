DROP INDEX series_one_queued;
DELETE FROM series WHERE status = 'queued';
ALTER TABLE series DROP CONSTRAINT series_status_check;
ALTER TABLE series ADD CONSTRAINT series_status_check
    CHECK (status IN ('active', 'stopped', 'completed'));
