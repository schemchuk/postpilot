# 001. Hosting: GitHub Actions cron, no server

Status: PROPOSED (Volodymyr decides)

## Context
Goal: about 0 EUR/month, runs in the cloud, laptop not in production. Load is a few jobs per week
plus a Telegram poll. Checked 2026-11-18 (snapdeploy.dev "State of Free Hosting 2026", Oracle docs).

## Options
1. **GitHub Actions (cron + dispatch) + free Postgres.** No server, public repo means free minutes,
   fits the CI/CD portfolio goal. Cons: cron granularity about 5 min and may be delayed, so Telegram
   approval latency is minutes, not seconds.
2. **Oracle Cloud Always Free VM.** Real always-on bot, instant replies. Cons: card and phone required,
   "out of capacity" is common, Always Free Arm was halved on 2026-06-15, idle instances (under 20 %
   use for 7 days, which our low load would hit) can be reclaimed.
3. **Fly.io / Render / Railway.** No real free tier any more (Fly ended it in 2024); costs a few EUR.

## Decision
Option 1. Latency is acceptable for an approval workflow. If minutes of latency prove annoying, a
webhook receiver can be added later without changing the core (the poller is just another entry point).

## Consequences
No long-running process; state must live outside the runner (ADR 003); workflow edits need OK.
