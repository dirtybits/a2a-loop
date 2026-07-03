# a2a-loop

Headless Codex <-> Claude Code workflow coordinator.

The loop uses local git state and `.a2a/` files as the default coordination
layer, then opens or updates a GitHub PR only after Claude approves the local
diff:

1. Claude plans.
2. Codex reviews and enhances the plan.
3. Claude approves the enhanced plan with `PLAN_STATUS: approved`.
4. Codex implements locally and commits.
5. Claude reviews the local diff.
6. Codex addresses local review comments.
7. Claude approves with `MERGE_DECISION: APPROVE`.
8. The coordinator pushes, opens or updates a PR, and can squash-merge when `--merge` is passed.

Those are the defaults. You can also choose `claude` or `codex` for the
planner, implementer, and reviewer roles.

## Requirements

- `codex` CLI with `codex exec`
- `claude` CLI with `claude -p`
- `gh` GitHub CLI authenticated for the target repo
- A target repository with an `origin` remote

## Install as a Local Command

Keep this repository separate from the projects it operates on, then put the
wrapper on your `PATH`:

```bash
mkdir -p ~/.local/bin
ln -s /Users/andy/Repos/a2a-loop/bin/a2a-loop ~/.local/bin/a2a-loop
```

Make sure `~/.local/bin` is on your shell `PATH`. After that, run the loop from
inside any target project:

```bash
a2a-loop \
  --goal "Implement the feature..." \
  --base main \
  --max-rounds 3 \
  --dry-run
```

`--repo` defaults to the current directory, so you only need to pass it when you
want to run the command from somewhere else:

```bash
a2a-loop \
  --repo /path/to/repo \
  --goal "Implement the feature..." \
  --dry-run
```

To execute an existing plan instead of creating one:

```bash
a2a-loop \
  --plan phase-9.plan.md \
  --dry-run
```

## Install as a Codex Skill

This repo also includes a thin companion skill at `skills/a2a-loop/`. The skill
does not replace the wrapper; it teaches Codex when and how to use the wrapper
without reloading the full project context.

Install it by symlinking the repo-local skill into your shared skills root:

```bash
ln -s /Users/andy/Repos/a2a-loop/skills/a2a-loop /Users/andy/.agents/skills/a2a-loop
```

After that, future Codex sessions can invoke `$a2a-loop` as procedural guidance
for running local-first Codex <-> Claude review loops.

## How It Works

`a2a-loop.py` is a small coordinator for a headless Codex <-> Claude Code
workflow. Local mode is the default because it saves context and avoids using
GitHub comments as scratch space. The loop keeps working memory in ignored
files under `.a2a/`:

```text
.a2a/
  plans/<goal-slug>.plan.md
  reviews/review-N.md
```

At a high level:

1. A human gives the loop a goal.
2. Claude writes `.a2a/plans/<goal-slug>.plan.md`.
3. Codex reviews the plan and adds repo-specific enhancements.
4. Claude reviews the enhanced plan.
5. If Claude emits `PLAN_STATUS: approved`, Codex implements locally and commits.
6. Claude reviews `git diff <base>...HEAD` and writes `.a2a/reviews/review-N.md`.
7. If Claude requests changes, Codex fixes them locally and commits.
8. The review/fix cycle repeats up to `--max-rounds`.
9. If Claude emits `MERGE_DECISION: APPROVE`, the coordinator pushes and opens or updates a PR.
10. If `--merge` was passed, the coordinator squash-merges the PR.

If `--plan path/to/existing.plan.md` is passed, the coordinator skips initial
plan creation and uses that file in place. It still runs implementer plan review
and reviewer plan approval unless `--skip-plan-review` is passed.

Use `--gh-review` when you explicitly want the older GitHub PR review surface.
In that mode, the coordinator pushes and opens or updates the PR before review,
Claude reviews the PR, and Codex pushes fixes back to the branch.

## Roles and Models

Default roles:

```text
--planner claude
--implementer codex
--reviewer claude
```

You can switch any role to `claude` or `codex`:

```bash
a2a-loop \
  --plan phase-9.plan.md \
  --planner codex \
  --implementer codex \
  --reviewer claude \
  --dry-run
```

Default model settings:

```text
--codex-model gpt-5.5
--codex-effort extra-high
--claude-model claude-fable-5
```

Claude effort is left to the Claude CLI default unless `--claude-effort` is
passed. Supported Claude effort values are `low`, `medium`, `high`, `xhigh`,
and `max`.

The script shells out to three CLIs:

```text
claude -p ...
codex exec ...
gh ...
```

The key safety idea is that neither agent directly merges. Claude can only
approve by printing the exact approval token, and the Python coordinator is the
only thing that may call `gh pr merge`, and only when `--merge` is explicitly
passed.

The important prompt-building and execution functions are:

- `build_plan_prompt(...)`: asks Claude to write a local plan file.
- `build_plan_review_prompt(...)`: asks Codex to improve the plan before implementation.
- `build_plan_approval_prompt(...)`: asks Claude to approve or refine the enhanced plan.
- `codex_exec(...)`: runs Codex with workspace-write sandboxing and approval disabled.
- `build_local_review_prompt(...)`: asks Claude to review the local diff and write `.a2a/reviews/review-N.md`.
- `build_local_fix_prompt(...)`: asks Codex to address local review feedback.
- `open_or_update_pr(...)`: pushes the branch, checks for an existing PR, and creates one if needed.
- `main()`: wires the bounded loop together.

## Dry Run

Always start with `--dry-run`:

```bash
./a2a-loop.py \
  --base main \
  --goal "Implement the feature..." \
  --max-plan-rounds 2 \
  --max-rounds 3 \
  --dry-run
```

## Real Run

```bash
./a2a-loop.py \
  --base main \
  --goal "Implement the feature..." \
  --max-plan-rounds 2 \
  --max-rounds 3
```

Add `--merge` only when you want the coordinator to squash-merge after the reviewer emits the exact approval token.
Add `--gh-review` when you want Claude and Codex to coordinate through GitHub
PR comments instead of local review files.
Add `--plan path/to/file.plan.md` when you already have a plan to execute.

## Safety

- Run on a clean branch or disposable worktree.
- `.a2a/` is ignored by default because it is local working memory.
- Keep `--max-plan-rounds` bounded.
- Keep `--max-rounds` bounded.
- Do not pass `--merge` until the dry-run and prompt contract look right.
- The coordinator fails closed unless Claude emits `MERGE_DECISION: APPROVE`.
- Logs are written to `a2a-logs/<timestamp>/run.log`.
