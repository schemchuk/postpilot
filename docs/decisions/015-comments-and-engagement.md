# 015. Comment monitoring, reply drafts and engagement log

Status: ACCEPTED (Volodymyr, 2026-11-20)

## Context

Spec F7.4 ports comment monitoring with AI reply drafts, an engagement log and `/stats` from
`work_in_Linkedin`. Volodymyr chose the scope on 2026-11-20: LinkedIn and Threads now, Instagram and
Facebook comments later (they need new Meta permissions and re-issued tokens).

- LinkedIn gives ordinary apps no API to read comments (`r_member_social` is closed). The old agent
  reads LinkedIn's notification emails from Gmail over IMAP with an app password, and that works.
- The Threads token is requested with `threads_read_replies`, so replies to postpilot's own Threads
  posts can be read from `GET /{media-id}/conversation`. The Threads media IDs of our posts are
  stored in `publications.remote_id`.
- Comments are written by third parties. Their text is untrusted input to the AI and personal data.
- The spec forbids publishing anything without approval in Telegram. A reply draft is only text
  for Volodymyr to copy; postpilot never posts a reply itself.

## Decision

1. **One job, `poll-engagement`, separate from `poll-telegram`.** A new workflow runs it every
   10 minutes. IMAP or Threads failures never block approvals in `poll-telegram`. Each source is
   isolated: one failing source does not stop the other, and the job exits nonzero if any failed.
2. **LinkedIn source: Gmail IMAP, read-only.** `IMAP_USER` / `IMAP_PASSWORD` (Gmail app password),
   host `imap.gmail.com`. `SELECT INBOX` read-only, `UID SEARCH FROM "linkedin.com"`, cursor
   `(uidvalidity, last_uid)` in `bot_state`. The first run (or a changed UIDVALIDITY) only stores
   the cursor and skips the mailbox history. At most 20 emails per run.
3. **Threads source: API.** For Threads publications from the last 14 days, read the conversation,
   skip replies by our own username, and record the rest.
4. **Engagement log in Postgres (migration 0010).** Table `engagement_events`: `id`, `channel`,
   `kind` (`comment`, `reaction`, `mention`, `connection`, `other`), `source_ref`, `actor`,
   `text`, `post_url`, `reply_draft`, `occurred_at`, `notified_at`, `telegram_message_id`,
   `created_at`, with `UNIQUE (channel, source_ref)`. `source_ref` is
   `imap:<uidvalidity>:<uid>` or the Threads reply ID. Insert is `ON CONFLICT DO NOTHING`, so a
   rerun never logs or notifies twice. Telegram is sent after the insert. Rows with
   `notified_at IS NULL` are retried on the next run.
5. **AI: one structured call per event.** For a LinkedIn email, Claude extracts `kind`, `actor`,
   `comment_text`, `post_url` and, for comments, a `reply_draft`. For a Threads reply only the draft
   is generated. The draft uses the language and the form of address of the comment, and the
   humanizer rules of ADR 007. The comment text sits in a delimited data block, and the prompt says
   that it is data, not instructions. Output is schema-validated; an invalid result is logged as
   kind `other` without a draft.
6. **Telegram message per comment.** Actor, channel, comment text, the draft in a tap-to-copy
   `<code>` block, an "Open post" URL button and an "Another variant" callback
   (`eng:redraft:<event_id>`, handled by `poll-telegram`, which edits the message). Reactions,
   connections and mentions are logged silently and show up only in `/stats`.
7. **`/stats` command.** Read in `poll-telegram`: a 30-day summary of engagement by channel and
   kind, short-link clicks (ADR 011), the latest GitHub traffic snapshot and published posts per
   channel.
8. **Data minimisation.** `text`, `actor` and `reply_draft` are set to NULL after 90 days;
   counts stay for `/stats`. Comment texts and email bodies are never logged.

Rollout as three tasks: T15a (migration, IMAP source, AI drafts, job and workflow, redraft
callback), T15b (Threads replies), T15c (`/stats` and the 90-day purge).

## Consequences

- Until the cutover (T18) both agents read the same mailbox, so a LinkedIn comment can produce two
  Telegram drafts. That is accepted for the side-by-side period.
- LinkedIn coverage depends on LinkedIn sending notification emails and on their HTML layout. The
  AI extraction tolerates layout changes better than regexes; a changed sender domain would need
  a code change.
- One more Gmail app password lives in GitHub Secrets (`IMAP_USER`, `IMAP_PASSWORD`).
- Instagram and Facebook comments are out of scope until new permissions are granted.
