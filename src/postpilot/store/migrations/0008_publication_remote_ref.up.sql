ALTER TABLE publications
    ADD COLUMN remote_ref text,
    ADD COLUMN remote_ref_claimed_at timestamptz;
