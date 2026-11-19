# 009. LinkedIn Posts API channel

Status: PROPOSED

The DE and UA adapters share the `linkedin` token provider and have separate publication
rows. Either language can succeed while the other is rejected. Missing or expired tokens
hold both channels until renewal.

Each post ends with a **visible** reference on its own line,
`postpilot:<item UUID>/<language>`. This adds 51 characters including the preceding
blank line. Validation reserves that space within the 3,000-character limit and rejects
oversize input. The reference distinguishes deliberate reuse of identical text and the
two language variants. Invisible characters could be normalized, and the existing
API integration provides no server-side idempotency key or private metadata field.
Approve the visible footer before enabling production publishing.

Recovery searches the authenticated author's posts via the Posts API author finder.
Member read access (`r_member_social`) and the finder's behavior for this app still need
live verification. A stale claim or ambiguous POST result permits lookup only. If the
finder returns no match, lacks permission, or the footer was removed, the claim stays
pending for reconciliation. A crash before the request was sent can also leave a pending
claim. A confirmed rejection can be retried independently.

Commentary escapes the legacy client's reserved punctuation while leaving `#`, `@`,
and explicit LinkedIn URN mention syntax intact. The adapter does not resolve account
names in plain-text mentions. Rendering and LinkedIn's treatment of escaped characters
near the length limit still need live verification.
