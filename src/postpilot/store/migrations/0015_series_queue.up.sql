ALTER TABLE series DROP CONSTRAINT series_status_check;
ALTER TABLE series ADD CONSTRAINT series_status_check
    CHECK (status IN ('active', 'queued', 'stopped', 'completed'));

CREATE UNIQUE INDEX series_one_queued ON series (status) WHERE status = 'queued';
