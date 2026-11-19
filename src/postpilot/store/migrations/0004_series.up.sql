CREATE TABLE series (
    id uuid PRIMARY KEY,
    repo text NOT NULL,
    part integer NOT NULL DEFAULT 1 CHECK (part > 0),
    total_parts integer CHECK (total_parts > 0),
    started_on date NOT NULL DEFAULT CURRENT_DATE,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'stopped', 'completed'))
);

CREATE UNIQUE INDEX series_one_active ON series (status) WHERE status = 'active';

CREATE TABLE series_parts (
    series_id uuid NOT NULL REFERENCES series (id),
    part integer NOT NULL CHECK (part > 0),
    text_de text NOT NULL,
    text_ua text NOT NULL,
    published_on date NOT NULL DEFAULT CURRENT_DATE,
    PRIMARY KEY (series_id, part)
);

CREATE TABLE posted_repos (
    repo text PRIMARY KEY,
    posted_at timestamptz NOT NULL DEFAULT now()
);
