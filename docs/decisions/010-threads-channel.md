# 010. Threads text channel

Status: ACCEPTED (Volodymyr, 2026-11-19)

Footer part superseded by ADR 013.

Threads publishes German text only, after explicit approval in Telegram. Each post
originally ended with a visible footer, `postpilot:<item UUID>/de`. Under ADR 013,
all 500 characters are available for text. Longer text is cut at the last sentence
end or word boundary that leaves room for an ellipsis.
If no boundary fits, it is cut at the 500-character limit. The adapter does not
rewrite the text with AI.

Publishing first creates a TEXT container, then publishes it with its creation ID.
The publication row stores the returned media ID. Recovery after an uncertain
response follows ADR 013 (stored container ID and status). A lookup miss leaves
the claim pending for reconciliation. A confirmed rejection can be retried
independently of LinkedIn.

Long-lived access tokens last 60 days and can be refreshed after 24 hours.
The daily token check tries to refresh them within 14 days of expiry. The initial
token can be imported, and assisted renewal uses the shared Pages callback.
