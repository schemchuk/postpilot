CREATE TABLE short_links (
    link_id text PRIMARY KEY,
    item_id uuid REFERENCES items (id),
    repo text NOT NULL,
    lang text NOT NULL,
    short_url text NOT NULL,
    destination text NOT NULL,
    created_on date NOT NULL
);

CREATE TABLE link_clicks (
    link_id text NOT NULL REFERENCES short_links (link_id),
    day date NOT NULL,
    clicks integer NOT NULL,
    PRIMARY KEY (link_id, day)
);

CREATE TABLE traffic_snapshots (
    repo text NOT NULL,
    day date NOT NULL,
    views integer NOT NULL,
    unique_views integer NOT NULL,
    clones integer NOT NULL,
    unique_clones integer NOT NULL,
    referrers jsonb NOT NULL,
    PRIMARY KEY (repo, day)
);
