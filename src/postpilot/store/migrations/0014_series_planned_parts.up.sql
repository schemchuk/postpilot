CREATE TABLE series_planned_parts (
    series_id uuid NOT NULL REFERENCES series (id),
    part integer NOT NULL CHECK (part > 0),
    text_de text NOT NULL,
    text_ua text NOT NULL,
    PRIMARY KEY (series_id, part)
);
