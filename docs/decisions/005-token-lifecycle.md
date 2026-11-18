# 005. Token lifecycle: tracking, silent refresh, assisted renewal

Status: PROPOSED (revised 2026-11-18 after Volodymyr's note that the old agent had auto-renewal)

## Context
The old agent (`work_in_Linkedin/token_manager.py`, `linkedin_auth.py`, `assistant_bot.py`) does four
things: stores the expiry date, warns 7 days ahead, tries a silent refresh if a refresh token exists,
and offers a "renew" button that opens the OAuth URL in the desktop browser and catches the callback
on `localhost:8080`. If the LinkedIn session is alive the consent screen passes without a click.
Facts: an ordinary LinkedIn app gets a 60-day access token and no refresh token, so a fully headless
renewal is impossible; the browser step cannot be removed, only made one tap. In the cloud there is
no desktop browser and no localhost, so the callback must be hosted somewhere.

## Decision
One `TokenManager` per provider behind a common interface, ported from the old code:
`days_left()`, `try_refresh()` (silent, no human), `renewal_url()`, `complete_renewal(code)`.

| Provider | Silent refresh | Assisted renewal |
|---|---|---|
| LinkedIn | only if LinkedIn ever returns a refresh token (code path kept, as in the old agent) | yes, every ~60 days |
| Threads, Instagram (long-lived tokens) | yes, via the provider's refresh endpoint, run when under ~30 days left | fallback if refresh fails |
| Facebook Page | page tokens derived from a long-lived token normally do not expire; checked via debug_token | fallback |

Flow for LinkedIn, no server needed:
1. `check-tokens` (daily cron) finds <= 7 days left, tries silent refresh, otherwise sends a Telegram
   message with a "Renew LinkedIn token" button (a link to the OAuth URL with a signed `state`).
2. Volodymyr taps it; with a live LinkedIn session consent is one tap.
3. The redirect URI is a static page on GitHub Pages (`/callback`) that shows the one-time code with
   a copy button and a deep link back to the bot. The code alone is useless without the client secret.
4. He sends the code to the bot (or taps the deep link); the next `poll-telegram` run verifies `state`,
   exchanges the code, stores the token and expiry, confirms in Telegram, and releases any items
   held back by the expired token (as the old agent did).
5. A local fallback `postpilot auth linkedin --open` keeps the old localhost flow for emergencies.

Storage: tokens and expiry dates live in the Postgres `tokens` table, encrypted with a key held in
GitHub Secrets (workflows cannot write GitHub Secrets with the default token, so the DB is the
writable place). Tokens are never logged.

## Consequences
- Expiry never surprises: alerts at 7 days, again daily, and at expiry with items held rather than
  failed.
- One new secret (`TOKEN_ENCRYPTION_KEY`), a static callback page, and one redirect URI to register in
  the LinkedIn app (guide and verified link provided when that task starts).
