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
5. Pass `--merge` only when the user explicitly wants the coordinator to squash-merge after Claude emits `MERGE_DECISION: APPROVE`.

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

## Safety Defaults

- Keep `.a2a/` as local working memory; it should normally be gitignored.
- Keep `--max-plan-rounds` and `--max-rounds` bounded.
- Prefer a clean branch or disposable worktree for target projects.
- Do not merge unless the exact `MERGE_DECISION: APPROVE` token appears.
- Inspect `a2a-logs/<timestamp>/run.log`, `.a2a/plans/`, and `.a2a/reviews/` when debugging a run.

## Tool Location

The local wrapper is expected to be available as `a2a-loop` on `PATH`. If it is
missing, check `/Users/andy/Repos/a2a-loop/bin/a2a-loop` and the installation
notes in `/Users/andy/Repos/a2a-loop/README.md`.
