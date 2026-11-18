CREATE TABLE items (
    id uuid PRIMARY KEY,
    source text NOT NULL CHECK (source IN ('github', 'telegram')),
    state text NOT NULL CHECK (state IN (
        'DRAFT', 'AWAITING_APPROVAL', 'APPROVED', 'PUBLISHING', 'PUBLISHED',
        'SKIPPED', 'FAILED'
    )),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    approved_by bigint,
    approved_at timestamptz
);

CREATE TABLE variants (
    item_id uuid NOT NULL REFERENCES items (id),
    lang text NOT NULL CHECK (lang IN ('de', 'ua')),
    text text NOT NULL,
    PRIMARY KEY (item_id, lang)
);

CREATE TABLE publications (
    item_id uuid NOT NULL REFERENCES items (id),
    channel text NOT NULL,
    status text NOT NULL CHECK (status IN ('PUBLISHING', 'PUBLISHED', 'FAILED')),
    remote_id text,
    error text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, channel)
);
