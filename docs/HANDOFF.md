# Handoff: Claude Code orchestrates Codex in the background

Volodymyr works in ONE Claude Code window. Claude launches Codex as a background process, so Volodymyr
never opens a Codex window. Same scheme as in handmake (Codex edit-only sandbox, Claude commits).

## Loop for each task Tnn
1. **Secret step (only if the task has a "Secret gate").** Claude gives the guide and a verified link;
   Volodymyr puts the secret into `.env` (and GitHub Secrets if CI needs it).
2. **Build (background).** Claude creates `feat/Tnn-<slug>`, writes the task brief from
   docs/03-tasks.md, and runs `codex exec -s workspace-write -C <repo> -o <scratch>/Tnn-last.md - < brief`
   with `run_in_background`. Codex edits files and runs local checks inside its sandbox (no network, no
   git push). Claude is notified when it exits.
3. **Check and review.** Claude runs ruff, mypy and pytest itself, reads the diff against the acceptance
   criteria, and sends findings back with `codex exec resume --last` (fix loop). Writer is Codex,
   reviewer is Claude, so the rule "author never merges own work" holds.
4. **Commit, PR, merge.** Claude commits with trailer `Agent: codex`, pushes, opens the PR, waits for CI,
   merges with `gh pr merge --squash --delete-branch`. Tasks marked "Needs OK" (workflows, secret
   handling) stop for Volodymyr's explicit OK before merge.
5. Claude updates STATUS, pushes, and tells Volodymyr to open a new window (see the next section). It does not start Tn+1 in the same window.

## No idle waiting (mandatory)
A background Codex run is never "wait for the notification". The notification only reports the launcher shell,
not Codex. Claude Code must:
- Launch with `setsid nohup ... &`, record the PID, and tell Codex in the brief that Claude is the orchestrator
  (HANDOFF authorizes it; do not stop on the Active-agent check, do not edit STATUS.md, do not ask questions).
- Within 2 minutes of launch, read the log tail and `git status` to confirm real progress.
- Then poll every 2-3 minutes with `kill -0 <pid>`, the `-o` last-message file and `git status`, using Monitor
  with an until-loop on the PID or the file (never `pgrep -f` matching its own command line).
- The moment the PID is gone or the last-message file exists, start review in the same turn: ruff, mypy,
  pytest, diff against acceptance criteria. Never end a turn with "waiting" while Codex is idle or finished.
- If the log is silent for 3 minutes, read it and act (resume Codex or take over the fix).
- Report to Volodymyr only facts (log, files, test output), never "it is working" without evidence.

## New window after every task (mandatory)
One task = one window. Once a task is merged and STATUS.md is updated and pushed, the agent STOPS and does
not start the next task in the same window. Its final message of the task is exactly this, in Ukrainian:

> Tnn готово і змерджено. Відкрий нове вікно і напиши: «continue per STATUS». Далі: Tn+1.

Before saying it the agent checks: working tree clean, STATUS.md names the next task and next step, everything
pushed to main. Nothing may live only in the chat. The new window starts from AGENTS.md and docs/STATUS.md.
The only exception is Volodymyr explicitly telling the agent to continue in the same window.

## Limits to know
- The Codex sandbox has no network: dependencies are installed by Claude beforehand, Codex does not
  push or call real APIs. Tests needing the network run outside the sandbox by Claude.
- Codex never gets secrets; they stay in `.env` and are read only by the running app.
- Manual fallback: Volodymyr can still open Codex himself with AGENTS.md as the entry point.
