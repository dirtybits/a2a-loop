# Codex + Claude Headless Agent Loop

## Shape

Use local git state and ignored `.a2a/` files as the default shared state
machine. Use GitHub PRs as the durable external gate after local approval, or
as the review surface when `--gh-review` is passed.

Default roles are Claude planner/reviewer and Codex implementer/fixer. The
coordinator may also assign any role to `claude` or `codex`.

1. **Human supplies target goal.**
2. **Claude plans.**
   - Returns an implementation plan, acceptance criteria, and test expectations.
   - The coordinator writes `.a2a/plans/<run-id>-<goal-slug>.plan.md`.
3. **Codex reviews the plan.**
   - Returns an `A2A_PLAN_APPEND` delta with repo-specific fixes, risks, and test improvements.
   - The coordinator appends the delta to the plan.
   - Does not implement yet.
4. **Claude approves the enhanced plan.**
   - Emits `PLAN_STATUS: approved`, or adds follow-up plan changes.
5. **Codex implements.**
   - Creates/uses a branch.
   - Makes code changes.
   - Runs tests.
   - Ends with `IMPLEMENTATION_READY` or `IMPLEMENTATION_STATUS: blocked`.
   - The coordinator commits only a ready result.
6. **Claude reviews local diff.**
   - Reads `git diff <base>...HEAD`.
   - Returns the review; the coordinator writes `.a2a/reviews/<run-id>/review-N.md`.
   - Emits a merge-clear decision or requests changes.
7. **Codex fixes.**
   - Reads local review files.
   - Addresses comments.
   - The coordinator commits only when the fixer reports ready and changed the worktree.
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
PLAN_STATUS: blocked
IMPLEMENTATION_READY
IMPLEMENTATION_STATUS: blocked
REVIEW_STATUS: changes_requested
A2A_REASON: concise decision reason
```

For the reviewer gate, require this exact line at the end of the reviewer's
output before merge (a small trailing window tolerates CLI footers):

```text
MERGE_DECISION: APPROVE
```

Anything else — including prose that merely mentions the token — keeps the
loop alive or fails closed.

The coordinator checkpoints a completed review before starting its fixer. A
blocked or no-progress fixer does not consume another review round; the
operator resolves the cause and explicitly resumes with `--retry-blocked`.
Plan reviewers use `PLAN_STATUS: blocked` when human authority is required,
and plan-review budget exhaustion creates the same explicit resumable stop.

## Plan-as-Contract

The plan file is the loop's contract, so the run must not have unrestricted
write access to its own contract. The coordinator owns every plan write
(agents return updates on stdout) and enforces:

- optimistic SHA-256 synchronization between an external source plan and the
  private run ledger: import source-only edits, export ledger-only edits, and
  block without modifying either file when both changed independently;

- Append-only body: updates that delete existing headings are rejected
  before touching the file.
- Constraint callouts (`SEQUENCING`, `DECISION`, `stop-the-line`,
  `founder-acked`) survive every update and are echoed into prompts and the
  PR body.
- A `## Closeout` section with `Verified:` / `Attempted-blocked (cause):` /
  `Deferred (tracked in):` / `Not claimed:` labels gates PR creation, so
  verification claims are structured and honest.

Direction of travel: make the plan's body sections literally read-only to
the run, with all run-time writes going to separate ledgers. The decision
log (`.a2a/runs/<run-id>/decisions.md`) is the first such ledger; todo
status and closeout could move there next, leaving the human-authored plan
body immutable for the run's lifetime.

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
- Keep `run.log` concise and put full turn transcripts under `logs/<run-id>/steps/`.
- Keep Claude review sessions on `dontAsk` with explicit read-only Git, PR
  inspection, and repository-test allowlists rather than unrestricted Bash.
- Let the coordinator run `gh pr merge --squash`, not the agents.
