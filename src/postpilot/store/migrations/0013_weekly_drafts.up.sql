CREATE TABLE weekly_drafts (
    week date PRIMARY KEY,
    item_id uuid REFERENCES items (id),
    repo text,
    series_id uuid REFERENCES series (id),
    part integer,
    facts jsonb NOT NULL DEFAULT '{}',
    outcome text NOT NULL CHECK (outcome IN ('drafted', 'skipped', 'no_activity')),
    created_at timestamptz NOT NULL DEFAULT now(),
    recorded_at timestamptz
);

CREATE UNIQUE INDEX weekly_drafts_item_id ON weekly_drafts (item_id) WHERE item_id IS NOT NULL;
