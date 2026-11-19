# 013. Threads idempotency without a visible footer

Status: ACCEPTED (Volodymyr, 2026-11-19; supersedes the footer part of ADR 010 for Threads only; LinkedIn and ADR 009 are unchanged)

## Context

ADR 010 appends `postpilot:<item UUID>/de` to every Threads post and recovers an uncertain
publish by scanning the account's posts for that suffix. The footer is publicly visible and
Volodymyr does not want it on Threads. Threads offers no idempotency key and no private
metadata field. Publishing is two calls: `POST /threads` creates a container and returns a
`creation_id`; `POST /threads_publish` publishes it. Only the second call can duplicate a post.
`GET /{creation_id}?fields=status` returns `FINISHED` (not yet published), `PUBLISHED`,
`IN_PROGRESS`, `ERROR` or `EXPIRED`. The docs do not say that it returns the media ID.

## Decision

1. Threads text is posted without any footer: the full 500 characters are available for text.
2. The container ID is stored in the publication row before `threads_publish` is called.
   Creating a container publishes nothing, so a crash before the ID is stored needs no recovery.
3. Recovery of an uncertain or stale claim reads the stored container's status:
   - `FINISHED`: not published; call `threads_publish` again with the same `creation_id`.
   - `PUBLISHED`: find the media ID by scanning recent posts for exact text equality
     with the text we sent, newest first, limited to posts created after the claim time.
     No match or several matches: the claim stays pending for manual reconciliation,
     as in ADR 010.
   - `IN_PROGRESS`: stay pending and retry on the next run.
   - `ERROR` or `EXPIRED`: create a new container and publish again.
4. LinkedIn keeps its visible footer (ADR 009) until Volodymyr decides otherwise.

## Consequences

- Needs a migration: a nullable `remote_ref` column on the publication row (store the container ID).
- `Channel` gains an optional step hook so the publisher can persist the container ID
  between the two Threads calls. The marker-based `find_existing` remains for LinkedIn.
- Open risk: the text-equality fallback after `PUBLISHED` is weaker than a unique marker.
  Two different items with identical text could be confused, so the time window and the
  single-match rule are mandatory. It needs one live check that `status` is readable
  after publishing.
- Alternative rejected: a self-reply that carries the marker. It is still public.
- Alternative rejected: invisible Unicode markers. Threads may normalise them, and ADR 009
  already rejected that approach.
