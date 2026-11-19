# 012. HeyGen rendering and Telegram approval

Status: ACCEPTED (Volodymyr, 2026-11-19)

Generate a spoken German script from the approved LinkedIn German variant. Before
submitting to HeyGen, check the UTC monthly render cap and wallet balance. Process
one item per run and serialize render jobs so simultaneous runs share the budget.

Persist the captioned MP4 in Postgres as `bytea` after burning in the AI-generated
label. Send that video to Telegram for a second approval from the configured user.
If the file exceeds the upload limit, keep it READY and send an alert without
approval buttons.

A failed render can be retried once by an explicit operator command. Report
interrupted RENDERING rows for investigation because HeyGen may already have
billed the request. Instagram and Facebook publishing is handled in T14.
