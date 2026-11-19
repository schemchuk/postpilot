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
