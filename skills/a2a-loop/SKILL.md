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

- Claude returns the plan in stdout; the coordinator persists `.a2a/plans/<run-id>-<goal-slug>.plan.md`.
- Codex and Claude return only `A2A_PLAN_APPEND` deltas during plan negotiation;
  the coordinator appends them without reprinting the full plan each round.
- Claude approves with `PLAN_STATUS: approved` or returns a coordinator-persisted follow-up delta.
- Codex implements locally; the coordinator commits only after an exact final
  `IMPLEMENTATION_READY` line.
- Claude reviews `git diff <base>...HEAD` in stdout; the coordinator persists `.a2a/reviews/<run-id>/review-N.md`.
- Codex fixes locally until Claude emits `MERGE_DECISION: APPROVE`.
- The coordinator commits fixes, then pushes and opens or updates the PR.
- Before creating a PR, the coordinator makes one final commit attempt and
  verifies the branch has commits ahead of base. If not, it reports that there
  is nothing to PR and prints `git log`, `git diff --stat`, and `git status`
  commands for the operator.

Use `--gh-review` only when the user wants GitHub PR comments to be the review
surface before approval.

## Roles and Models

Keep the default split unless the user asks otherwise:

```text
--planner claude
--implementer codex
--reviewer claude
```

Supported role values are `claude` and `codex`. By default, `a2a-loop` resolves
concrete model and effort values before the run starts, prints each value with
its source, and shows them in every `[agent:<agent>:<model>:<effort>]` trace
line. Codex defaults come from `~/.codex/config.toml`; Claude defaults come from
`~/.claude/settings.json`, with Claude effort falling back to the coordinator
default `high` when the settings file does not expose one. Override per run with
`--codex-model`, `--codex-effort`, `--claude-model`, and `--claude-effort`, or use
`A2A_CODEX_MODEL`, `A2A_CODEX_EFFORT`, `A2A_CLAUDE_MODEL`, and
`A2A_CLAUDE_EFFORT`. Codex effort supports
`minimal|low|medium|high|xhigh|max|ultra`; GPT-5.6 Terra supports
`low|medium|high|xhigh|max|ultra`, and `extra-high` maps to `xhigh`. Claude
effort supports `low|medium|high|xhigh|max`. Claude uses local claude.ai
login/subscription auth by default; pass `--claude-use-api-key` or set
`A2A_CLAUDE_USE_API_KEY=1` only when API-key billing is intentional. The
coordinator prints `Codex auth status:` from `codex login status` and `Claude
auth status:` from `claude auth status --json` at startup when applicable.

## Safety Defaults

- Keep `.a2a/` as local working memory; the coordinator adds it to `.gitignore`.
- Expect default branches like `a2a/<plan-or-goal-slug>-<yyyymmdd>`.
- If `--branch` names an existing branch, the coordinator should check it out
  without resetting it.
- Keep legacy `a2a-logs/` ignored for runs created by older coordinator versions.
- Keep `--max-plan-rounds` and `--max-rounds` bounded.
- Treat `IMPLEMENTATION_STATUS: blocked` as a hard stop: the coordinator must
  checkpoint, surface `A2A_REASON:`, and never commit or start review.
- If a requested fix creates no commit, stop as blocked/no-progress instead of
  reviewing the unchanged diff again.
- Prefer a clean branch or disposable worktree for target projects.
- New branches start from `origin/<base>`; agent PRs are never stacked on
  unmerged work.
- The plan body is append-only: the coordinator rejects plan updates that
  delete existing sections, and `SEQUENCING`/`DECISION`/`stop-the-line`/
  `founder-acked` callouts are echoed into prompts and the PR body as hard
  constraints.
- The plan must carry a `## Closeout` section (`Verified:`,
  `Attempted-blocked (cause):`, `Deferred (tracked in):`, `Not claimed:`)
  before the coordinator opens a PR; the reviewer verifies statuses match
  reality and writes a reviewer briefing that lands in the PR body.
- Do not merge unless the exact `MERGE_DECISION: APPROVE` token appears.
- Prefer merging manually or from a separate session. `--merge` refuses
  same-agent implementer/reviewer runs (self-merge guard) and blocks while
  `gh pr checks` reports failing or pending checks on the head SHA.
- Watch the terminal for defaults, artifact paths, agent step, handoff,
  approval, PR, and merge status.
- Use `--verbose` or `A2A_VERBOSE=1` when the operator wants a summarized live
  trace of public non-code agent text, tool calls, stderr, and post-turn
  diffstats. Full per-turn output stays under `.a2a/logs/<run-id>/steps/`.
- Claude reviewer turns use `dontAsk` with a narrow allowlist for read-only Git,
  PR inspection, and standard test runners, never unrestricted Bash.
- Existing plans outside `.a2a/` are copied into `.a2a/plans/` as the run
  ledger. SHA-256 optimistic synchronization imports source-only operator
  edits, exports ledger-only changes, and blocks concurrent divergence without
  overwriting either copy.
- Inspect the concise `.a2a/logs/<timestamp>/run.log`, raw
  `.a2a/logs/<timestamp>/steps/`, `.a2a/runs/<run-id>/decisions.md`,
  `.a2a/plans/`, and `.a2a/reviews/` when debugging a run.
- Local review stdout is persisted to `.a2a/reviews/<run-id>/review-N.md`;
  reviewers are not required to write review files directly.
- Initial plan stdout, negotiation `A2A_PLAN_APPEND` deltas, and implementation
  `A2A_PLAN_UPDATE` blocks are coordinator-persisted; agents are not required
  to write `.a2a` plan files directly.
- Optional `A2A_COMMIT_MESSAGE` blocks let agents suggest a commit subject; the
  coordinator still creates the commit and falls back to a phase-derived message.
- Real runs checkpoint to `.a2a/runs/<run-id>/state.json`; use
  `a2a-loop --resume <run-id>` to continue from the next incomplete phase.
  Use bare `a2a-loop --resume` to resume the newest checkpoint under
  `.a2a/runs/`, equivalent to picking the latest `ls -td .a2a/runs/* | head`.
- Reviews are checkpointed before the fixer starts. A blocked checkpoint does
  not rerun implicitly; after resolving the blocker, use
  `a2a-loop --resume <run-id> --retry-blocked`.
- A plan reviewer uses `PLAN_STATUS: blocked` for human-only decisions. Plan
  review budget exhaustion is also checkpointed as blocked instead of leaving
  a dangling `plan_written` state.
- Older checkpoints with a persisted changes-requested review and no recorded
  fix are migrated to the pending-fix phase on resume, avoiding a duplicate
  reviewer turn.
- On resume, `--max-plan-rounds` and `--max-rounds` add another bounded batch
  from the saved next round. Explicitly passed role/model/effort flags
  override the checkpoint and are echoed as `resume override:` trace lines.
  Resume preserves saved verbose mode; pass `--no-verbose` to disable it.
- Approval tokens must be an exact line at the end of the reviewer output;
  prose that merely mentions a token does not approve.
- Each agent turn times out after `A2A_AGENT_TIMEOUT_SECONDS` (default 3600;
  `0` disables).
- Fatal agent output, such as unsupported-model API errors, should halt the
  loop immediately with captured stdout/stderr visible to the operator.

## Tool Location

The local wrapper is expected to be available as `a2a-loop` on `PATH`. If it is
missing, find the checked-out `a2a-loop` repository and symlink its
`bin/a2a-loop` wrapper into a directory on `PATH`. See the repository README
for install notes.
