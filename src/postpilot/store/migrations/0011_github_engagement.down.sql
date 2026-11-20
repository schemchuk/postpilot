DELETE FROM engagement_events WHERE kind IN ('star', 'fork');
ALTER TABLE engagement_events DROP CONSTRAINT engagement_events_kind_check;
ALTER TABLE engagement_events ADD CONSTRAINT engagement_events_kind_check
    CHECK (kind IN ('comment', 'reaction', 'mention', 'connection', 'other'));
