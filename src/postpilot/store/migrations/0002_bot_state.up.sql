CREATE TABLE bot_state (
    key text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE pending_edits (
    user_id bigint PRIMARY KEY,
    item_id uuid NOT NULL REFERENCES items (id),
    lang text NOT NULL CHECK (lang IN ('de', 'ua')),
    created_at timestamptz NOT NULL DEFAULT now()
);
