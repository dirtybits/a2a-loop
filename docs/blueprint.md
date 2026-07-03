# Codex + Claude Headless Agent Loop

## Shape

Use GitHub as the shared state machine:

1. **Human supplies target goal.**
2. **Claude plans.**
   - Produces an implementation plan, acceptance criteria, and test expectations.
   - Does not edit code in this phase.
3. **Codex implements.**
   - Creates/uses a branch.
   - Makes code changes.
   - Runs tests.
   - Pushes branch and opens/updates a PR.
4. **Claude reviews PR.**
   - Reads PR diff.
   - Leaves actionable comments or emits a merge-clear decision.
5. **Codex fixes.**
   - Reads PR comments.
   - Addresses comments.
   - Pushes updates.
6. **Loop repeats until clear.**
7. **Coordinator squash-merges.**

## Recommended Role Split

Claude should be the planner/reviewer because it can stay comparatively detached from the implementation and behave like a product/maintainer gate.

Codex should be the implementer/fixer because it is strong at repo-local edits, test runs, and iterative repair.

## Coordination Contract

Every agent turn should produce machine-checkable markers:

```text
PLAN_READY
IMPLEMENTATION_READY
REVIEW_STATUS: changes_requested
REVIEW_STATUS: approved
```

For the reviewer gate, require this exact line before merge:

```text
MERGE_DECISION: APPROVE
```

Anything else keeps the loop alive or fails closed.

## Safety Rails

- Run in a disposable git worktree or clean branch.
- Use bounded rounds, for example `--max-rounds 4`.
- Prefer `--dry-run` until prompts and branch behavior look right.
- Never auto-merge if tests failed.
- Never auto-merge without the exact approval token.
- Keep a full log directory per run.
- Let the coordinator run `gh pr merge --squash`, not the agents.

