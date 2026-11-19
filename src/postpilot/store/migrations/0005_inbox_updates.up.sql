CREATE TABLE inbox_updates (
    update_id bigint PRIMARY KEY,
    item_id uuid NOT NULL REFERENCES items (id)
);
