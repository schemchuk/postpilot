# 005. LinkedIn app reuse and token renewal

Status: PROPOSED

Context: Volodymyr reuses the existing LinkedIn app (Client ID/Secret) and states it in the portfolio.
Ordinary apps get a 60-day access token without a refresh token.
Decision: postpilot runs its own OAuth authorization and stores its own token; the old token is never
copied. `check-tokens` alerts in Telegram 7 days before expiry with an authorization link; Volodymyr
completes the browser step; the callback is a one-off local run of `postpilot auth linkedin`, which
writes the new token to GitHub Secrets via `gh secret set`. A hosted callback is not built.
Open: redirect URI to add in the LinkedIn app (guide with link will be provided when this task starts).
