# Codex + Claude Headless Agent Loop

## Shape

Use local git state and ignored `.a2a/` files as the default shared state
machine. Use GitHub PRs as the durable external gate after local approval, or
as the review surface when `--gh-review` is passed.

1. **Human supplies target goal.**
2. **Claude plans.**
   - Writes `.a2a/plans/<goal-slug>.plan.md`.
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
   - Writes `.a2a/reviews/review-N.md`.
   - Emits a merge-clear decision or requests changes.
7. **Codex fixes.**
   - Reads local review files.
   - Addresses comments.
   - Commits locally.
8. **Loop repeats until clear.**
9. **Coordinator pushes and opens/updates a PR.**
10. **Coordinator squash-merges when requested.**

## Recommended Role Split

Claude should be the planner/reviewer because it can stay comparatively detached from the implementation and behave like a product/maintainer gate.

Codex should be the implementer/fixer because it is strong at repo-local edits, test runs, and iterative repair.

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

For the reviewer gate, require this exact line before merge:

```text
MERGE_DECISION: APPROVE
```

Anything else keeps the loop alive or fails closed.

## Safety Rails

- Run in a disposable git worktree or clean branch.
- Keep `.a2a/` ignored unless a project intentionally wants to commit agent working memory.
- Use bounded plan rounds, for example `--max-plan-rounds 2`.
- Use bounded rounds, for example `--max-rounds 4`.
- Prefer `--dry-run` until prompts and branch behavior look right.
- Never auto-merge if tests failed.
- Never auto-merge without the exact approval token.
- Keep a full log directory per run.
- Let the coordinator run `gh pr merge --squash`, not the agents.
