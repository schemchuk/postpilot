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
5. Claude updates STATUS and tells Volodymyr: "Tnn done, starting Tn+1" (or what is needed from him).

## When a new window is useful
Only to keep context small: after a few tasks Claude says "Tnn done; open a new window and say
'continue per STATUS'". STATUS.md carries everything, nothing is lost.

## Limits to know
- The Codex sandbox has no network: dependencies are installed by Claude beforehand, Codex does not
  push or call real APIs. Tests needing the network run outside the sandbox by Claude.
- Codex never gets secrets; they stay in `.env` and are read only by the running app.
- Manual fallback: Volodymyr can still open Codex himself with AGENTS.md as the entry point.
