# Codex + Claude Headless Agent Loop

## Shape

Use local git state and ignored `.a2a/` files as the default shared state
machine. Use GitHub PRs as the durable external gate after local approval, or
as the review surface when `--gh-review` is passed.

Default roles are Claude planner/reviewer and Codex implementer/fixer. The
coordinator may also assign any role to `claude` or `codex`.

1. **Human supplies target goal.**
2. **Claude plans.**
   - Writes `.a2a/plans/<run-id>-<goal-slug>.plan.md`.
   - Produces an implementation plan, acceptance criteria, and test expectations.
3. **Codex reviews the plan.**
   - Adds repo-specific fixes, risks, and test improvements to the plan.
   - Does not implement yet.
4. **Claude approves the enhanced plan.**
   - Emits `PLAN_STATUS: approved`, or adds follow-up plan changes.
5. **Codex implements.**
   - Creates/uses a branch.
   - Makes code changes.
   - Runs tests.
   - Commits locally.
6. **Claude reviews local diff.**
   - Reads `git diff <base>...HEAD`.
   - Writes `.a2a/reviews/<run-id>/review-N.md`.
   - Emits a merge-clear decision or requests changes.
7. **Codex fixes.**
   - Reads local review files.
   - Addresses comments.
   - Commits locally.
8. **Loop repeats until clear.**
9. **Coordinator pushes and opens/updates a PR.**
10. **Coordinator squash-merges when requested.**

When `--plan <path>` is passed, skip initial plan creation and use that existing
`.plan.md` file as shared state. Still run implementer plan review and reviewer
approval unless `--skip-plan-review` is passed.

## Recommended Role Split

Claude should be the planner/reviewer because it can stay comparatively detached from the implementation and behave like a product/maintainer gate.

Codex should be the implementer/fixer because it is strong at repo-local edits, test runs, and iterative repair.

For same-backend experiments, keep the roles explicit:

```text
--planner codex --implementer codex --reviewer claude
```

Avoid arbitrary command templates until the `claude|codex` role switch proves
too small.

## Coordination Contract

Every agent turn should produce machine-checkable markers:

```text
PLAN_READY
PLAN_REVIEW_READY
PLAN_STATUS: approved
PLAN_STATUS: changes_requested
IMPLEMENTATION_READY
REVIEW_STATUS: changes_requested
```

For the reviewer gate, require this exact line at the end of the reviewer's
output before merge (a small trailing window tolerates CLI footers):

```text
MERGE_DECISION: APPROVE
```

Anything else — including prose that merely mentions the token — keeps the
loop alive or fails closed.

## Safety Rails

- Run in a disposable git worktree or clean branch.
- Use human-readable run branches like `a2a/<plan-or-goal-slug>-<yyyymmdd>`.
- Do not reset existing branches during setup; check them out as-is.
- Keep `.a2a/` ignored unless a project intentionally wants to commit agent working memory.
- Use bounded plan rounds, for example `--max-plan-rounds 2`.
- Use bounded rounds, for example `--max-rounds 4`.
- Use `--plan existing.plan.md` when executing an existing implementation-ready plan.
- Prefer `--dry-run` until prompts and branch behavior look right.
- Never auto-merge if tests failed.
- Never auto-merge without the exact approval token.
- Keep a full log directory per run.
- Let the coordinator run `gh pr merge --squash`, not the agents.
