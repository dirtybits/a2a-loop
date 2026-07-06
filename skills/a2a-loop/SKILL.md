---
name: a2a-loop
description: Use when the user wants to run, configure, debug, or explain the local Codex <-> Claude Code PR loop using the a2a-loop wrapper. Triggers include requests to coordinate Claude planning/review with Codex implementation/fixes, run local agent review loops, use .a2a plan/review files, choose between local review and --gh-review, or safely open/merge PRs after agent approval.
---

# a2a-loop

Use the wrapper as the executable tool and this skill as the operating guide.
Do not vendor the `a2a-loop` repo into target projects by default.

## Default Workflow

1. Work from the target project directory when possible.
2. Start with a dry run:

```bash
a2a-loop --goal "Implement the feature..." --base main --max-plan-rounds 2 --max-rounds 3 --dry-run
```

3. For a real local-first run, remove `--dry-run`.
4. Pass `--repo /path/to/repo` only when running from outside the target project.
5. Pass `--merge` only when the user explicitly wants the coordinator to squash-merge after the reviewer emits `MERGE_DECISION: APPROVE`.

## Existing Plans

When the user already has a plan such as `phase-9.plan.md`, prefer first-class
plan execution over turning the filename into a vague goal:

```bash
a2a-loop --plan phase-9.plan.md --dry-run
```

Add `--goal "..."` only when useful as supplemental intent. Use
`--skip-plan-review` only when the user explicitly wants to bypass implementer
plan review and reviewer approval.

## Plan Files

Use `$plan-writing` (`dirtybits/agent-skills/plan-writing`) for generated and
reviewed `.plan.md` files. The a2a coordinator stores working plans under
`.a2a/plans/`, but they should still follow the plan-writing convention:

- YAML frontmatter with `name`, `overview`, `todos`, and `isProject`.
- Todo ids that are stable, lowercase, and hyphenated.
- Todo statuses of `pending`, `in_progress`, or `completed`.
- Concrete files, commands, verification gates, and blockers.
- Updated statuses/body when implementation diverges from the original plan.

## Review Modes

Prefer the default local review mode. It saves tokens and avoids using GitHub
comments as scratch space:

- Claude writes `.a2a/plans/<goal-slug>.plan.md`.
- Codex enhances the plan.
- Claude approves with `PLAN_STATUS: approved`.
- Codex implements locally and commits.
- Claude reviews `git diff <base>...HEAD` and writes `.a2a/reviews/review-N.md`.
- Codex fixes locally until Claude emits `MERGE_DECISION: APPROVE`.
- The coordinator then pushes and opens or updates the PR.

Use `--gh-review` only when the user wants GitHub PR comments to be the review
surface before approval.

## Roles and Models

Keep the default split unless the user asks otherwise:

```text
--planner claude
--implementer codex
--reviewer claude
```

Supported role values are `claude` and `codex`. By default, `a2a-loop` lets the
Codex and Claude CLIs use their configured model defaults. Override per run
with `--codex-model`, `--codex-effort`, `--claude-model`, and
`--claude-effort`, or use `A2A_CODEX_MODEL`, `A2A_CODEX_EFFORT`,
`A2A_CLAUDE_MODEL`, and `A2A_CLAUDE_EFFORT`. Codex effort supports
`minimal|low|medium|high`; compatibility aliases `extra-high`, `xhigh`, and
`max` map to `high`. Claude effort supports `low|medium|high|xhigh|max`.

## Safety Defaults

- Keep `.a2a/` as local working memory; it should normally be gitignored.
- Keep `a2a-logs/` gitignored because it contains run logs.
- Keep `--max-plan-rounds` and `--max-rounds` bounded.
- Prefer a clean branch or disposable worktree for target projects.
- Do not merge unless the exact `MERGE_DECISION: APPROVE` token appears.
- Watch the terminal for agent step, handoff, approval, PR, and merge status.
- Inspect `a2a-logs/<timestamp>/run.log`, `.a2a/plans/`, and `.a2a/reviews/`
  when debugging a run.
- Local review stdout is persisted to `.a2a/reviews/review-N.md` if the
  reviewer could not write the file directly.
- Real runs checkpoint to `.a2a/runs/<run-id>/state.json`; use
  `a2a-loop --resume <run-id>` to continue from the next incomplete phase.
- On resume, `--max-plan-rounds` and `--max-rounds` add another bounded batch
  from the saved next round.
- Fatal agent output, such as unsupported-model API errors, should halt the
  loop immediately with captured stdout/stderr visible to the operator.

## Tool Location

The local wrapper is expected to be available as `a2a-loop` on `PATH`. If it is
missing, find the checked-out `a2a-loop` repository and symlink its
`bin/a2a-loop` wrapper into a directory on `PATH`. See the repository README
for install notes.
