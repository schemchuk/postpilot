# 004. Approval as an explicit state machine

Status: ACCEPTED (Volodymyr, 2026-11-18)

Decision: items move only through DRAFT, AWAITING_APPROVAL, APPROVED, PUBLISHING, PUBLISHED, SKIPPED,
FAILED. `publish` accepts only APPROVED items and the transition is enforced in `core/states.py`, so
"no auto-publishing" is a code invariant covered by tests, not a convention. Approvals record the
Telegram user id and time. The video has its own second approval before Reels.
