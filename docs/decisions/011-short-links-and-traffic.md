# ADR 011 — Short links and traffic snapshots

Status: PROPOSED

Use Short.io links to attribute clicks to individual posts and languages. Keep the
original URL available when link creation or storage fails.

Store link metadata and daily snapshots in Postgres so scheduled runs can share
history beyond GitHub's rolling traffic window. Keep cumulative clicks per link
and repository traffic per UTC snapshot day. A rerun replaces that day's values.
The click summary uses the latest cumulative count for each link.
