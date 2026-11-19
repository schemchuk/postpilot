# 010. Threads text channel

Status: PROPOSED (Volodymyr approval required)

Threads publishes German text only, after explicit approval in Telegram. Each post
ends with a visible footer, `postpilot:<item UUID>/de`. The footer and preceding
blank line use 51 of the 500 characters, leaving 449 for the text. Longer text
is cut at the last sentence end or word boundary that leaves room for an ellipsis.
If no boundary fits, it is cut at the character limit reserved for text. The
adapter does not rewrite the text with AI.

Publishing first creates a TEXT container, then publishes it with its creation ID.
The publication row stores the returned media ID. After an uncertain response,
the publisher searches the authenticated account's posts by footer, following
cursor pagination. A lookup miss leaves the claim pending for reconciliation.
A confirmed rejection can be retried independently of LinkedIn.

Long-lived access tokens last 60 days and can be refreshed after 24 hours.
The daily token check tries to refresh them within 14 days of expiry. The initial
token can be imported, and assisted renewal uses the shared Pages callback.
