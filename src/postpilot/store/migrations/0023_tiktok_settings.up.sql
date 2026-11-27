CREATE TABLE tiktok_settings (
    item_id uuid PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    privacy_level text NOT NULL,
    allow_comment boolean NOT NULL DEFAULT false,
    allow_duet boolean NOT NULL DEFAULT false,
    allow_stitch boolean NOT NULL DEFAULT false,
    chosen_at timestamptz NOT NULL DEFAULT now()
);
