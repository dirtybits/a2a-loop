# a2a-loop

Headless Codex <-> Claude Code workflow coordinator.

> Experimental: this can create branches, run agents, commit changes, push PRs,
> and optionally merge. Start with `--dry-run`, use disposable worktrees or
> clean branches, and inspect generated plans/reviews before trusting a run.

The loop uses local git state and `.a2a/` files as the default coordination
layer, then opens or updates a GitHub PR only after Claude approves the local
diff:

1. Claude plans.
2. Codex reviews and enhances the plan.
3. Claude approves the enhanced plan with `PLAN_STATUS: approved`.
4. Codex implements locally; the coordinator commits the resulting diff.
5. Claude reviews the local diff.
6. Codex addresses local review comments.
7. Claude approves with `MERGE_DECISION: APPROVE`.
8. The coordinator pushes, opens or updates a PR, and can squash-merge when `--merge` is passed.

Those are the defaults. You can also choose `claude` or `codex` for the
planner, implementer, and reviewer roles.

## Requirements

- Python 3.11+
- `codex` CLI with `codex exec`
- `claude` CLI with `claude -p`
- `gh` GitHub CLI authenticated for the target repo
- A target repository with an `origin` remote

## Install as a Local Command

Keep this repository separate from the projects it operates on, then put the
wrapper on your `PATH`:

```bash
mkdir -p ~/.local/bin
git clone https://github.com/YOUR_GITHUB_USERNAME/a2a-loop.git ~/src/a2a-loop
ln -s ~/src/a2a-loop/bin/a2a-loop ~/.local/bin/a2a-loop
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

## Install as an Agent Skill

This repo also includes a thin companion skill at `skills/a2a-loop/`. The skill
does not replace the wrapper; it gives both Codex and Claude shared procedural
guidance for when and how to use the wrapper without reloading the full project
context.

Install it by symlinking the repo-local skill into your shared skills root:

```bash
mkdir -p ~/.agents/skills
ln -s ~/src/a2a-loop/skills/a2a-loop ~/.agents/skills/a2a-loop
```

After that, future agent sessions can invoke `$a2a-loop` as procedural guidance
for running local-first Codex <-> Claude review loops. Codex and Claude can both
use the same skill text when they need to understand the coordinator workflow.

## How It Works

`a2a-loop.py` is a small coordinator for a headless Codex <-> Claude Code
workflow. Local mode is the default because it saves context and avoids using
GitHub comments as scratch space. The loop keeps working memory in ignored
files under `.a2a/`:

```text
.a2a/
  plans/<run-id>-<goal-slug>.plan.md
  reviews/<run-id>/review-N.md
  runs/<run-id>/state.json
  runs/<run-id>/decisions.md
  logs/<run-id>/run.log
  logs/<run-id>/steps/step-NN-<agent>-<phase>.log
```

Plans and reviews are namespaced by run id so concurrent or repeated runs with
similar goals never share ledgers or reviews.

At a high level:

1. A human gives the loop a goal.
2. Claude returns the plan in stdout; the coordinator persists `.a2a/plans/<run-id>-<goal-slug>.plan.md`.
3. Codex reviews the plan and returns only an `A2A_PLAN_APPEND` delta; the coordinator appends it.
4. Claude reviews the enhanced plan and may return another coordinator-persisted append delta.
5. If Claude emits `PLAN_STATUS: approved`, Codex implements locally. The coordinator commits only when the final line is exactly `IMPLEMENTATION_READY`.
6. Claude reviews `git diff <base>...HEAD` in stdout; the coordinator persists it to `.a2a/reviews/<run-id>/review-N.md`.
7. If Claude requests changes, Codex fixes them locally and the coordinator commits the resulting diff.
8. The review/fix cycle repeats up to `--max-rounds`.
9. If Claude emits `MERGE_DECISION: APPROVE`, the coordinator pushes and opens or updates a PR.
10. If `--merge` was passed, the coordinator squash-merges the PR.

An implementer or fixer can instead end with `IMPLEMENTATION_STATUS: blocked`.
The coordinator checkpoints and prints the reason without committing or starting
another review. A requested fix that produces no commit also stops as
`blocked/no_progress` rather than buying another review of the same diff.

If `--plan path/to/existing.plan.md` is passed, the coordinator skips initial
plan creation and uses that file in place. It still runs implementer plan review
and reviewer plan approval unless `--skip-plan-review` is passed.

## Plan Contract

The plan file is the run's contract, and the coordinator enforces it
mechanically:

- **Append-only body.** Agents may update todo statuses and append dated
  progress, divergence, and closeout notes, but a plan update that deletes an
  existing heading (Goal, Scope, Rollback, decision notes, ...) is rejected
  before anything touches the file, and the run stops with instructions.
- **Constraint markers are a control channel.** Lines carrying `SEQUENCING`,
  `DECISION`, `stop-the-line`, or `founder-acked` are extracted from the plan,
  injected into implementer/fixer prompts as hard constraints, and echoed in
  the PR body under "Constraints acknowledged".
- **Machine-checkable closeout.** The implementer must maintain a
  `## Closeout` section with four labels — `Verified:`,
  `Attempted-blocked (cause):`, `Deferred (tracked in):`, `Not claimed:` —
  and the coordinator refuses to open a PR without it. Todos may be
  `completed` only when every done-when item is met; the reviewer is told to
  treat overclaimed statuses as changes to request.
- **Reviewer briefing.** On approval, the reviewer writes a briefing for the
  human or external reviewer (riskiest hunks, invariants, what to try to
  break, what internal rounds already caught); it is copied into the PR body.

Use `--gh-review` when you explicitly want the older GitHub PR review surface.
In that mode, the coordinator pushes and opens or updates the PR before review,
Claude reviews the PR, then Codex fixes locally and the coordinator commits and
pushes fixes back to the branch.

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

By default, `a2a-loop` resolves concrete model and effort defaults before the
run starts, prints each value with its source, and uses those values in every
`[agent:<agent>:<model>:<effort>]` trace line. Codex defaults come from
`~/.codex/config.toml`; Claude defaults come from `~/.claude/settings.json`,
with Claude effort falling back to `high` when the settings file does not expose
one. Override models and effort per run:

```bash
a2a-loop \
  --goal "Implement the feature..." \
  --codex-model gpt-5.6-terra \
  --codex-effort max \
  --claude-model claude-fable-5 \
  --dry-run
```

Or set environment variables:

```bash
export A2A_CODEX_MODEL=gpt-5.6-terra
export A2A_CODEX_EFFORT=max
export A2A_CLAUDE_MODEL=claude-fable-5
```

Available model-related flags:

```text
--codex-model gpt-5.6-terra
--codex-effort max
--claude-model claude-fable-5
--claude-effort high
```

Codex effort resolves from `A2A_CODEX_EFFORT`, then
`model_reasoning_effort` in `~/.codex/config.toml`. Supported Codex effort
values are `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`.
GPT-5.6 Terra supports `low|medium|high|xhigh|max|ultra`; `max` is the
recommended implementation default. For compatibility, `extra-high` maps to
`xhigh`.

Claude effort resolves from `A2A_CLAUDE_EFFORT`, then `effort` in
`~/.claude/settings.json`, then the coordinator default `high`. Supported
Claude effort values are `low`, `medium`, `high`, `xhigh`, and `max`.

By default, `a2a-loop` runs Claude through the local `claude` login/subscription
auth path by stripping API-key auth environment variables from Claude
subprocesses. Pass `--claude-use-api-key` or set `A2A_CLAUDE_USE_API_KEY=1`
when you intentionally want API-key billing.

Codex uses the local Codex CLI auth selected with `codex login`. At startup, the
coordinator prints `Codex auth status:` from `codex login status`. Current Codex
CLI versions may report the auth mode, such as ChatGPT or API key, without
exposing the exact account email.

Use `claude auth login` to choose the Anthropic account/subscription used by
headless `claude -p` runs. At startup, the coordinator prints `Claude auth
status:` from `claude auth status --json` so you can confirm whether Claude is
logged in and, when the CLI reports it, which account is active. In API-key
mode, that account check is skipped because the subprocess inherits
`ANTHROPIC_*` auth instead.

The script shells out to three CLIs:

```text
claude -p ...
codex exec ...
gh ...
```

The key safety idea is that agents edit product/source files but do not own
control-plane persistence. They do not write `.a2a` plans/reviews, `.git`, push,
or merge. Claude can only approve by printing the exact approval token, and the
Python coordinator is the only thing that may persist `.a2a` artifacts, create
commits, push, or call `gh pr merge`; merging only happens when `--merge` is
explicitly passed.

The important prompt-building and execution functions are:

- `build_plan_prompt(...)`: asks Claude to return a complete plan for coordinator persistence.
- `build_plan_review_prompt(...)`: asks Codex to return an append-only plan delta before implementation.
- `build_plan_approval_prompt(...)`: asks Claude to approve or return an append-only follow-up delta.
- `codex_exec(...)`: runs Codex with workspace-write sandboxing and approval disabled.
- `build_local_review_prompt(...)`: asks Claude to review the local diff in stdout so the coordinator can persist `.a2a/reviews/<run-id>/review-N.md`.
- `build_local_fix_prompt(...)`: asks Codex to address local review feedback.
- `open_or_update_pr(...)`: checks for an existing PR, verifies the branch has
  commits ahead of base, pushes the branch, and creates one if needed. If the
  branch has no commits ahead of base, it stops with inspection commands instead
  of surfacing GitHub's raw `No commits between` GraphQL error.
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

## Resume

Real runs checkpoint after each completed phase to:

```text
.a2a/runs/<run-id>/state.json
```

If a run exits early, resume it from the same target repo:

```bash
a2a-loop --resume <run-id>
```

To resume the newest run under `.a2a/runs/`, omit the run id:

```bash
a2a-loop --resume
```

That picks the same newest checkpoint you would find with:

```bash
ls -td .a2a/runs/* | head
```

You can also pass the state file path directly. On resume, the coordinator
checks out the saved branch, appends to the original log, reuses the saved plan
and role settings, and continues from the next incomplete phase. If the earlier
run stopped after exhausting review rounds, `--max-rounds` on resume adds
another bounded batch of rounds instead of restarting at `review-1.md`.
Completed reviews are checkpointed before their fixer starts, so resuming a
pending fix does not rerun the reviewer.
For checkpoints created by older a2a-loop versions, resume also recognizes a
persisted changes-requested review with no matching fix decision and migrates
it to the pending-fix phase automatically.

Blocked checkpoints do not retry implicitly. Resolve the reported blocker, then run:

```bash
a2a-loop --resume <run-id> --retry-blocked
```

Explicitly passed flags override the checkpoint on resume: `--planner`,
`--implementer`, `--reviewer`, `--codex-model`, `--codex-effort`,
`--claude-model`, `--claude-effort`, `--claude-use-api-key`, `--verbose`, and
`--no-verbose`. Each applied override is printed as a `resume override:` trace
line. Defaults resolved from config files do not override the checkpoint, so a
plain `--resume` keeps the run's original settings. If a saved run has
`verbose=true`, use `--no-verbose` to resume quietly.

## Safety

- Run on a clean branch or disposable worktree.
- Default branches are named `a2a/<plan-or-goal-slug>-<yyyymmdd>`.
- New branches start from `origin/<base>` (after a fetch), falling back to the
  local base ref, so agent PRs are never stacked on unmerged work or stale
  local state. Squash-merge repos punish stacked branches with conflict
  surgery.
- Passing `--branch` checks out an existing branch if present, or creates it if
  missing; it does not reset an existing branch.
- Every run starts with a capability manifest trace: CLI availability, repo
  writability, origin reachability, Codex code-mode host availability, and both
  agents' auth status — so a run
  never discovers mid-turn that a dependency is missing. The implementer is
  also told to pre-classify plan verification steps as runnable or blocked up
  front instead of discovering them mid-run.
- `.a2a/` is ignored by default because it is local working memory.
- `.a2a/` and legacy `a2a-logs/` are added to `.gitignore` automatically after
  branch setup, so the change stays isolated to the run branch.
- Keep `--max-plan-rounds` bounded.
- Keep `--max-rounds` bounded.
- Do not pass `--merge` until the dry-run and prompt contract look right.
  Prefer merging from a separate session or by hand: the loop's job ends at an
  open, well-briefed PR.
- `--merge` refuses to run when the implementer and reviewer are the same
  agent (self-merge guard): the author of a change must not be its only merge
  gate.
- Before an auto-merge, the coordinator runs `gh pr checks` and blocks the
  merge while checks are failing or pending on the head SHA — a green
  deployment preview alone is never treated as CI evidence. Re-trigger
  skipped workflows by closing and reopening the PR.
- The coordinator fails closed unless Claude emits `MERGE_DECISION: APPROVE`.
- Implementation and fix turns fail closed unless their final line is exactly
  `IMPLEMENTATION_READY` or `IMPLEMENTATION_STATUS: blocked`.
- Claude review phases retain `dontAsk` and receive a narrow allowlist for
  read-only Git/PR inspection and standard repository test commands. They do
  not receive unrestricted Bash or edit tools.
- Approval tokens must appear as an exact line at the end of the reviewer's
  output (a small trailing window tolerates CLI footers); reviewer prose that
  merely quotes a token is not an approval.
- The terminal shows defaults, artifact paths, agent steps, handoffs, approval,
  PR, and merge actions.
- Pass `--verbose` or set `A2A_VERBOSE=1` for a summarized live trace: public
  non-code agent text, tool calls, stderr, and a post-turn worktree diffstat.
  Code snippets and raw tool output stay in the per-turn step logs instead of
  the terminal or coordinator log.
- Existing plans outside `.a2a/` are copied into `.a2a/plans/` as the run
  ledger, and coordinator-persisted plan updates sync back to the source plan.
- `.a2a/logs/<timestamp>/run.log` is the concise coordinator log. Full prompts,
  JSON events, stdout, stderr, and tool output live in per-turn files under
  `.a2a/logs/<timestamp>/steps/`, keeping the main log useful for `tail -f`.
- Decision logs are written to `.a2a/runs/<run-id>/decisions.md` with concise
  `A2A_REASON:` values, resolutions, and commit hashes.
- Local review stdout is persisted to `.a2a/reviews/<run-id>/review-N.md`;
  reviewers are not required to write review files directly.
- Initial plan stdout, negotiation `A2A_PLAN_APPEND` deltas, and implementation
  `A2A_PLAN_UPDATE` blocks are coordinator-persisted; agents are not required
  to write `.a2a` plan files directly.
- Optional `A2A_COMMIT_MESSAGE` blocks let agents suggest a commit subject; the
  coordinator still creates the commit and falls back to a phase-derived message.
- Fatal agent output, such as unsupported-model API errors, stops the loop
  immediately and prints the captured stdout/stderr for the operator.
- Each agent turn is bounded by a timeout (default 3600 seconds). Set
  `A2A_AGENT_TIMEOUT_SECONDS` to adjust it, or to `0` to disable.

## Tests

Unit tests cover helper behavior plus blocker/no-progress control flow,
pending-fix resume, reviewer permissions, plan deltas, and host preflight:

```bash
python3 -m unittest discover tests
```

## License

MIT
