# STATUS

- Active agent: claude-code (orchestrating Codex, docs/HANDOFF.md)
- Current task: T13e (PR #22, ADR 013 ACCEPTED) merged 2026-11-19: Threads posts have no visible footer; the container ID is stored before `threads_publish` (migration 0008, applied on job start) and recovery reads the container status, falling back to an exact-text match within claim time minus 5 min. Live check pending: is `GET /{container}?fields=status` readable after publishing (verify on the next real Threads post). Next window: T13f fix the Neon pooler search_path leak (see INCIDENT under Database), then T14.
- (older note) T13b (+ hotfixes T13c #19, T13d #20) is merged and verified live: first video rendered (26.7 s, cost $1.01, wallet now $22.92) and APPROVED in Telegram on 2026-11-19.
- (older note) T13b is merged (PR #18: render-video CLI + workflow, script prompt, budget gate, Telegram video approval, ADR 012 ACCEPTED). Next: T14.
- Done: T01 skeleton + CI (#1), T02 store + models + state machine (#2), T03 publisher + fake channel (#3),
  T04 Telegram notifier + approval (#4), T05a token core (#5), T05b LinkedIn assisted renewal + Pages
  callback (#6), T06 CLI jobs + failure alerts + scheduled workflows (#7), T06a hotfix: migrations on job start, failure type to stderr (#8), T07 AI client + prompts + humanizer (#9), T11 LinkedIn channel DE+UA (#12), T12 Threads channel + token manager + import (#13; ADR 010 ACCEPTED), T13a HeyGen client + label + render store (#17; migration 0007), T13b render-video pipeline (#18), T09 short.io links + daily traffic snapshot (#16; migration 0006; ADR 011 ACCEPTED), T10 Telegram inbox S2 (#11, ANTHROPIC_API_KEY now passed to poll-telegram), T08 GitHub source + series + repo safety (#10; migration 0004 applied on job start). 333 tests green incl. integration
  tests against Neon. Pages live: https://schemchuk.github.io/postpilot/callback/. Redirect URI is registered
  in the LinkedIn app (Volodymyr confirmed).
- Branch: main
- Last commit: see `git log -1`
- Next step: T13f (pooler search_path leak, small test-infra fix), then T14 Instagram + Facebook Reels (secret gates: IG and FB tokens, one at a time). render-video was triggered once via workflow_dispatch on 2026-11-19 to verify it live and succeeded as a no-op (production DB had no items, no HeyGen credits spent): workflow, secrets, ffmpeg and DB access are verified, the real render was then verified live (see Current task). Follow-ups for T14: failed renders after a paid `create_video` are not counted in the monthly cap (count_this_month excludes FAILED); script validation (80-120 words) has no automatic retry. HEYGEN_* secrets are in GitHub Secrets. Short links are not yet wired into publishing.
- ADR 009 ACCEPTED (Volodymyr, 2026-11-19): visible footer `postpilot:<uuid>/<lang>` on LinkedIn posts. LinkedIn channel is untested against live LinkedIn (no token issued yet; needs r_member_social check).
- Blocked on: nothing. Verified 2026-11-18 19:01 EET: secret `DATABASE_URL` fixed, migrations 0001-0003 applied
  to the production Neon database by hand, `check-tokens` and `poll-telegram` workflows green.
- Follow-ups: purge unused oauth nonces in bot_state; local fallback `postpilot auth linkedin --open`;
  run integration tests in CI (needs a workflow change + OK); LinkedIn token has not been issued yet, the first
  `/renew` flow is untested against real LinkedIn (check-tokens sends a renewal prompt, status "missing").
- Database: Neon free Postgres (Frankfurt), `DATABASE_URL` is in `.env` and in GitHub Secrets (unquoted,
  never `source` `.env`). Integration tests run with `TEST_DATABASE_URL` in a throwaway schema; they are
  skipped in CI until a workflow change adds the secret (needs Volodymyr's explicit OK).
  INCIDENT 2026-11-19: `DATABASE_URL` is a Neon `-pooler` (PgBouncer transaction mode) URL. The test fixture's
  session-level `SET search_path TO test_<hex>` leaked into a pooled server backend, so every new connection got a
  dropped test schema as search_path (production queries would fail "relation does not exist"). Fixed by hand with
  `RESET ALL` on that backend; no scheduled job ran in the window. Until fixed in code (next task, before any other
  integration-test run): run integration tests only against the direct (non-pooler) host, make `connect(schema=...)`
  refuse `-pooler` hosts, and have production `connect()` not depend on session state.
- Notes: T03 recovers stale PUBLISHING claims via the channel marker lookup (`find_existing`); real
  adapters must implement it. A sender still running past `stale_after` can race the lookup.
- Secrets: ANTHROPIC_API_KEY, LinkedIn app credentials, DATABASE_URL are set. Telegram bot token and chat id are set
  (bot @volodymyr_postpilot_bot). TOKEN_ENCRYPTION_KEY is set (copy in a password
  manager is recommended). Later: HeyGen key/avatar, Meta app, Threads, IG, FB credentials.
- Decisions: git user.name stays `schemchuk`. Volodymyr allowed `gh pr merge` for the agent (project .claude/settings.json) and on 2026-11-19 stated the agent should always merge task PRs after green CI (standing authorization, including PRs that touch workflows once he has set the secret).
- Pending cleanup: delete GitHub repo social_bridge and "lumara" Projects (confirm first).

- Threads: Meta app `postpilot-2` (app id 1198565332850126, Threads app id in .env). The first app had a Meta bug (tester invite stuck without Accept); a new app fixed it. Token is generated by hand in the dashboard (User Token Generator), valid 60 days; check-tokens refreshes it once imported.

- Follow-ups (2026-11-19 live run): (1) [fixed in #21] a stale or late Telegram callback makes `answer_callback_query`/edit raise TelegramError AFTER the DB change, so poll-telegram exits 1; make post-commit Telegram replies non-fatal. (2) render-video exits 0 even when a render FAILED (only a Telegram alert); consider a nonzero exit. (3) Avatar changed 2026-11-19: new look `Volodymyr social vertical` (d2577111..., group Volodymyr, generated by prompt from the desk look, 9:16 head-and-shoulders, cost $1.00) is now `HEYGEN_AVATAR_ID` in `.env` and GitHub Secrets; voice unchanged. Rendered and APPROVED by Volodymyr on 2026-11-19 (35 s, he liked the motion and framing); wallet now $20.6. The first video used the old look (174911d2...). Volodymyr disliked the old arms-crossed look (stiff motion). Engine stays avatar_iv (motion is fine). (4) the first test item was published to Threads for real.
- Follow-up (2026-11-19, Volodymyr's request): the idempotency marker `postpilot:<uuid>/<lang>` is appended to the post text and is publicly visible on Threads (and LinkedIn per ADR 009). `find_existing` finds a post by `text.endswith(suffix)` (channels/threads.py, channels/base.py `marker`). Volodymyr does not want it visible on Threads. Options: store the Threads `creation_id`/post ID before `threads_publish` and recover through the API without a text marker, or post the marker as a hidden/self-reply. Decide in an ADR (supersedes part of ADR 009 for Threads), keep LinkedIn behaviour unless he asks otherwise. DONE 2026-11-19: (1) post-commit Telegram replies non-fatal (PR #21; non-blocking leftover: the 'Regeneration is unavailable' answer in approval.py is still unwrapped). (2) DONE: marker removed from Threads (ADR 013, PR #22). Next: T13f pooler fix, then T14.
- Threads profile (done by Volodymyr by hand, 2026-11-19): handle `@volodymyrshemchuk_`, German bio, GitHub link, profile Public.
