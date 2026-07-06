#!/usr/bin/env python3
"""
Headless agent-to-agent PR loop for Codex and Claude Code.

By default, Claude plans/reviews and Codex critiques/implements/fixes. Local
git state plus `.a2a/` files are the default coordination layer; GitHub PR
review is available with --gh-review. Start with --dry-run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import textwrap
from dataclasses import dataclass


APPROVAL_TOKEN = "MERGE_DECISION: APPROVE"
PLAN_APPROVAL_TOKEN = "PLAN_STATUS: approved"
PLAN_CHANGES_TOKEN = "PLAN_STATUS: changes_requested"
REVIEW_CHANGES_TOKEN = "REVIEW_STATUS: changes_requested"
AGENTS = ("claude", "codex")
CODEX_EFFORTS = ("minimal", "low", "medium", "high")
CODEX_EFFORT_ALIASES = {
    "extra-high": "high",
    "xhigh": "high",
    "max": "high",
}
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def env_default(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value


def normalize_codex_effort(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = CODEX_EFFORT_ALIASES.get(value, value)
    if normalized not in CODEX_EFFORTS:
        accepted = [*CODEX_EFFORTS, *CODEX_EFFORT_ALIASES]
        raise SystemExit(
            "Codex effort must be one of: "
            + ", ".join(accepted)
            + ". Note: xhigh/max are Claude effort names and map to Codex high."
        )
    return normalized


@dataclass
class CmdResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class WorkflowTrace:
    log: pathlib.Path
    step: int = 0
    active_agent: str | None = None

    def event(self, message: str) -> None:
        line = f"[loop] {message}"
        print(line)
        self._write(line)

    def start_agent(self, agent: str, phase: str, artifact: str | None = None) -> int:
        if self.active_agent and self.active_agent != agent:
            self.event(f"handoff: {self.active_agent} -> {agent}")
        self.active_agent = agent
        self.step += 1
        suffix = f" ({artifact})" if artifact else ""
        line = f"[agent:{agent}] step {self.step} start: {phase}{suffix}"
        print(line)
        self._write(line)
        return self.step

    def finish_agent(self, agent: str, step: int, phase: str, output: str) -> None:
        detail = f", output {len(output)} chars" if output else ""
        line = f"[agent:{agent}] step {step} done: {phase}{detail}"
        print(line)
        self._write(line)

    def _write(self, line: str) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as f:
            f.write(f"{line}\n")


def run(args: list[str], cwd: pathlib.Path, dry_run: bool, log_file: pathlib.Path) -> CmdResult:
    rendered = " ".join(shlex.quote(a) for a in args)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"\n\n$ {rendered}\n")

    if dry_run:
        print(f"[dry-run] {rendered}")
        with log_file.open("a", encoding="utf-8") as f:
            f.write("[dry-run] skipped\n")
        return CmdResult(args=args, returncode=0, stdout="", stderr="")

    proc = subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with log_file.open("a", encoding="utf-8") as f:
        if proc.stdout:
            f.write(proc.stdout)
        if proc.stderr:
            f.write("\n[stderr]\n")
            f.write(proc.stderr)
    return CmdResult(args=args, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def require_ok(result: CmdResult, context: str) -> None:
    if result.returncode != 0:
        rendered = " ".join(shlex.quote(a) for a in result.args)
        raise SystemExit(f"{context} failed ({result.returncode}): {rendered}\n{result.stderr}")


def claude_print(
    prompt: str,
    repo: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
    model: str | None,
    effort: str | None,
) -> str:
    args = [
        "claude",
        "-p",
        "--permission-mode",
        "dontAsk",
        "--output-format",
        "text",
    ]
    if model:
        args.extend(["--model", model])
    if effort:
        args.extend(["--effort", effort])
    args.append(prompt)

    result = run(
        args,
        cwd=repo,
        dry_run=dry_run,
        log_file=log,
    )
    require_ok(result, "Claude turn")
    return result.stdout


def codex_exec(
    prompt: str,
    repo: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
    model: str | None,
    effort: str | None,
) -> str:
    args = [
        "codex",
        "exec",
        "-C",
        str(repo),
        "--sandbox",
        "workspace-write",
        "-c",
        'approval_policy="never"',
    ]
    if model:
        args.extend(["--model", model])
    if effort:
        args.extend(["-c", f'model_reasoning_effort="{effort}"'])
    args.append(prompt)

    result = run(
        args,
        cwd=repo,
        dry_run=dry_run,
        log_file=log,
    )
    require_ok(result, "Codex turn")
    return result.stdout


def run_agent(
    agent: str,
    phase: str,
    prompt: str,
    repo: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
    trace: WorkflowTrace,
    codex_model: str | None,
    codex_effort: str | None,
    claude_model: str | None,
    claude_effort: str | None,
    artifact: str | None = None,
) -> str:
    step = trace.start_agent(agent, phase, artifact)
    if agent == "claude":
        output = claude_print(prompt, repo, dry_run, log, claude_model, claude_effort)
        trace.finish_agent(agent, step, phase, output)
        return output
    if agent == "codex":
        output = codex_exec(prompt, repo, dry_run, log, codex_model, codex_effort)
        trace.finish_agent(agent, step, phase, output)
        return output
    raise ValueError(f"Unsupported agent: {agent}")


def gh_json(repo: pathlib.Path, args: list[str], dry_run: bool, log: pathlib.Path) -> dict:
    result = run(["gh", *args], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(result, "gh command")
    if dry_run or not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def gh_text(repo: pathlib.Path, args: list[str], dry_run: bool, log: pathlib.Path) -> str:
    result = run(["gh", *args], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(result, "gh command")
    return result.stdout


def current_branch(repo: pathlib.Path, dry_run: bool, log: pathlib.Path) -> str:
    result = run(["git", "branch", "--show-current"], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(result, "branch detection")
    return result.stdout.strip() or "a2a/dry-run"


def ensure_branch(repo: pathlib.Path, branch: str, dry_run: bool, log: pathlib.Path) -> None:
    result = run(["git", "checkout", "-B", branch], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(result, "branch setup")


def slugify_goal(goal: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")
    return slug[:64].strip("-") or "a2a-plan"


def repo_relative(repo: pathlib.Path, path: pathlib.Path) -> str:
    return path.relative_to(repo).as_posix()


def ensure_a2a_dirs(repo: pathlib.Path, dry_run: bool) -> None:
    if dry_run:
        return
    (repo / ".a2a" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / ".a2a" / "reviews").mkdir(parents=True, exist_ok=True)


def read_if_present(path: pathlib.Path, fallback: str = "") -> str:
    if not path.exists():
        return fallback
    return path.read_text(encoding="utf-8")


def resolve_repo_path(repo: pathlib.Path, value: pathlib.Path) -> pathlib.Path:
    path = value.expanduser()
    if not path.is_absolute():
        path = repo / path
    path = path.resolve()
    try:
        path.relative_to(repo)
    except ValueError as exc:
        raise SystemExit(f"Path must be inside repo {repo}: {path}") from exc
    return path


def open_or_update_pr(
    repo: pathlib.Path,
    base: str,
    branch: str,
    goal: str,
    plan: str,
    dry_run: bool,
    log: pathlib.Path,
) -> str:
    push_result = run(["git", "push", "-u", "origin", branch], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(push_result, "branch push")
    existing = gh_json(
        repo,
        ["pr", "list", "--head", branch, "--json", "number,url", "--limit", "1"],
        dry_run,
        log,
    )
    if dry_run:
        return "DRY_RUN_PR"
    if isinstance(existing, list) and existing:
        return existing[0]["url"]

    body = textwrap.dedent(
        f"""
        ## Goal

        {goal}

        ## Agent Plan

        {plan.strip()}

        ## Coordination

        This PR was opened by the a2a-loop coordinator.
        """
    ).strip()
    return gh_text(
        repo,
        [
            "pr",
            "create",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            f"A2A: {goal[:72]}",
            "--body",
            body,
        ],
        dry_run,
        log,
    ).strip()


def build_plan_prompt(goal: str, base: str, plan_path: str) -> str:
    return f"""
You are the planner in an agent-to-agent workflow.

Target goal:
{goal}

Base branch: {base}

Write the implementation plan to:
{plan_path}

Use the dirtybits/agent-skills `plan-writing` convention for `.plan.md` files:
- Start with YAML frontmatter containing `name`, `overview`, `todos`, and `isProject`.
- Make `todos` a short checklist with stable lowercase hyphenated `id` values,
  concrete `content`, and `status: pending`.
- After frontmatter, include Markdown sections for goal, scope, files to change,
  implementation steps, verification, rollout/rollback if relevant, and blockers.
- Include enough repo-specific detail that the implementer can proceed without guessing.

Do not edit source files. End your response with PLAN_READY.
""".strip()


def build_plan_review_prompt(goal: str, base: str, plan_path: str) -> str:
    return f"""
You are the implementer, reviewing the plan before implementation.

Goal:
{goal}

Base branch: {base}

Read the plan at:
{plan_path}

Inspect the repo enough to catch missing steps, risky assumptions, weak tests,
or repo-specific implementation details. Update the plan file in place by
adding an "Implementer Review" or "Implementation Enhancements" section. Preserve the
plan-writing frontmatter shape and keep todo ids stable.

Do not implement the feature yet. End your response with PLAN_REVIEW_READY.
""".strip()


def build_plan_approval_prompt(goal: str, base: str, plan_path: str) -> str:
    return f"""
You are the reviewer/plan gate, approving the implementation plan before code edits begin.

Goal:
{goal}

Base branch: {base}

Review the enhanced plan at:
{plan_path}

If the plan is ready for implementation, end with exactly:
{PLAN_APPROVAL_TOKEN}

If changes are still needed, update the plan file in place with a concise
"Reviewer Follow-up" section and end with exactly:
{PLAN_CHANGES_TOKEN}

Do not implement the feature.
""".strip()


def build_implement_prompt(goal: str, plan_path: str, base: str) -> str:
    return f"""
You are the implementer in an agent-to-agent workflow.

Goal:
{goal}

Plan file:
{plan_path}

Instructions:
- Read the plan file before editing.
- Inspect the repo before editing.
- Maintain plan todo statuses as work progresses: set started todos to
  `in_progress` and completed, verified todos to `completed`.
- Implement the smallest complete change that satisfies the plan.
- Run relevant tests/checks.
- Commit your changes to the current branch.
- Do not push.
- Do not merge.
- End your final response with IMPLEMENTATION_READY or explain the blocker.

Base branch: {base}
""".strip()


def build_local_review_prompt(goal: str, base: str, plan_path: str, review_path: str) -> str:
    return f"""
You are the reviewer/merge gate in a local-first agent-to-agent workflow.

Goal:
{goal}

Plan file:
{plan_path}

Review the local branch diff against base branch `{base}`. Prefer local git
state over conversation history. Useful commands include:

- git status
- git log --oneline {base}..HEAD
- git diff {base}...HEAD

Write your review to:
{review_path}

If changes are needed:
- Include actionable findings in the review file.
- End your response with exactly:
{REVIEW_CHANGES_TOKEN}

If the implementation satisfies the goal and tests are adequate:
- Write a concise approval summary in the review file.
- End your response with exactly:
{APPROVAL_TOKEN}

Do not merge, push, or edit source files.
""".strip()


def build_local_fix_prompt(goal: str, base: str, plan_path: str, review_path: str) -> str:
    return f"""
You are the fixer in a local-first agent-to-agent workflow.

Goal:
{goal}

Plan file:
{plan_path}

Review file:
{review_path}

Instructions:
- Read the plan and review files.
- Inspect the local diff against `{base}`.
- Maintain plan todo statuses as work progresses.
- Address all actionable review comments.
- Run relevant tests/checks.
- Commit fixes to the current branch.
- Do not push.
- Do not merge.
- End with IMPLEMENTATION_READY or explain the blocker.
""".strip()


def build_gh_review_prompt(goal: str, pr_url: str) -> str:
    return f"""
You are the reviewer/merge gate in an agent-to-agent workflow.

Goal:
{goal}

PR:
{pr_url}

Review the PR diff and current branch. Use GitHub CLI if available to inspect the PR.

If changes are needed:
- Leave actionable PR comments or a clear review summary.
- End with REVIEW_STATUS: changes_requested.

If the PR satisfies the goal and tests are adequate:
- End with exactly:
{APPROVAL_TOKEN}

Do not merge the PR.
""".strip()


def build_gh_fix_prompt(goal: str, pr_url: str, review: str) -> str:
    return f"""
You are the fixer in an agent-to-agent workflow.

Goal:
{goal}

PR:
{pr_url}

Reviewer output:
{review}

Instructions:
- Inspect PR comments and the diff.
- Address all actionable comments.
- Run relevant tests/checks.
- Commit and push fixes to the same branch.
- Do not merge.
- End with IMPLEMENTATION_READY or explain the blocker.
""".strip()


def negotiate_plan(
    repo: pathlib.Path,
    goal: str,
    base: str,
    plan_path: pathlib.Path,
    planner: str,
    implementer: str,
    reviewer: str,
    max_plan_rounds: int,
    skip_plan_review: bool,
    dry_run: bool,
    log: pathlib.Path,
    trace: WorkflowTrace,
    codex_model: str | None,
    codex_effort: str | None,
    claude_model: str | None,
    claude_effort: str | None,
    create_plan: bool,
) -> str:
    plan_rel = repo_relative(repo, plan_path)
    if create_plan:
        trace.event(f"planning starts: {planner} writes {plan_rel}")
        run_agent(
            planner,
            "write implementation plan",
            build_plan_prompt(goal, base, plan_rel),
            repo,
            dry_run,
            log,
            trace,
            codex_model,
            codex_effort,
            claude_model,
            claude_effort,
            artifact=plan_rel,
        )
    else:
        trace.event(f"using existing plan: {plan_rel}")

    if skip_plan_review:
        trace.event("plan review skipped")
        return read_if_present(plan_path, "DRY_RUN_PLAN")

    for round_index in range(1, max_plan_rounds + 1):
        trace.event(f"plan review round {round_index}/{max_plan_rounds}")
        run_agent(
            implementer,
            "review and enhance plan",
            build_plan_review_prompt(goal, base, plan_rel),
            repo,
            dry_run,
            log,
            trace,
            codex_model,
            codex_effort,
            claude_model,
            claude_effort,
            artifact=plan_rel,
        )
        approval = run_agent(
            reviewer,
            "approve plan",
            build_plan_approval_prompt(goal, base, plan_rel),
            repo,
            dry_run,
            log,
            trace,
            codex_model,
            codex_effort,
            claude_model,
            claude_effort,
            artifact=plan_rel,
        )
        if dry_run:
            approval = PLAN_APPROVAL_TOKEN
        if PLAN_APPROVAL_TOKEN in approval:
            return read_if_present(plan_path, "DRY_RUN_PLAN")

    raise SystemExit(f"Plan was not approved within {max_plan_rounds} rounds. See {log}")


def run_local_review_loop(
    repo: pathlib.Path,
    goal: str,
    base: str,
    plan_path: pathlib.Path,
    reviewer: str,
    implementer: str,
    max_rounds: int,
    dry_run: bool,
    log: pathlib.Path,
    trace: WorkflowTrace,
    codex_model: str | None,
    codex_effort: str | None,
    claude_model: str | None,
    claude_effort: str | None,
) -> bool:
    plan_rel = repo_relative(repo, plan_path)
    for round_index in range(1, max_rounds + 1):
        review_path = repo / ".a2a" / "reviews" / f"review-{round_index}.md"
        review_rel = repo_relative(repo, review_path)
        trace.event(f"local review round {round_index}/{max_rounds}: {review_rel}")
        review = run_agent(
            reviewer,
            "review local diff",
            build_local_review_prompt(goal, base, plan_rel, review_rel),
            repo,
            dry_run,
            log,
            trace,
            codex_model,
            codex_effort,
            claude_model,
            claude_effort,
            artifact=review_rel,
        )
        if dry_run:
            review = APPROVAL_TOKEN
        if APPROVAL_TOKEN in review:
            trace.event(f"review approved by {reviewer}")
            return True

        run_agent(
            implementer,
            "fix local review comments",
            build_local_fix_prompt(goal, base, plan_rel, review_rel),
            repo,
            dry_run,
            log,
            trace,
            codex_model,
            codex_effort,
            claude_model,
            claude_effort,
            artifact=review_rel,
        )

    return False


def run_gh_review_loop(
    repo: pathlib.Path,
    goal: str,
    pr_url: str,
    reviewer: str,
    implementer: str,
    max_rounds: int,
    dry_run: bool,
    log: pathlib.Path,
    trace: WorkflowTrace,
    codex_model: str | None,
    codex_effort: str | None,
    claude_model: str | None,
    claude_effort: str | None,
) -> bool:
    for round_index in range(1, max_rounds + 1):
        trace.event(f"GitHub review round {round_index}/{max_rounds}: {pr_url}")
        review = run_agent(
            reviewer,
            "review GitHub PR",
            build_gh_review_prompt(goal, pr_url),
            repo,
            dry_run,
            log,
            trace,
            codex_model,
            codex_effort,
            claude_model,
            claude_effort,
            artifact=pr_url,
        )
        if dry_run:
            review = APPROVAL_TOKEN
        if APPROVAL_TOKEN in review:
            trace.event(f"GitHub review approved by {reviewer}")
            return True
        run_agent(
            implementer,
            "fix GitHub review comments",
            build_gh_fix_prompt(goal, pr_url, review),
            repo,
            dry_run,
            log,
            trace,
            codex_model,
            codex_effort,
            claude_model,
            claude_effort,
            artifact=pr_url,
        )
        gh_text(
            repo,
            ["pr", "comment", pr_url, "--body", f"Implementer pushed fixes for round {round_index}."],
            dry_run,
            log,
        )

    return False


def main() -> int:
    default_codex_model = env_default("A2A_CODEX_MODEL")
    default_codex_effort = env_default("A2A_CODEX_EFFORT")
    default_claude_model = env_default("A2A_CLAUDE_MODEL")
    default_claude_effort = env_default("A2A_CLAUDE_EFFORT")
    if default_claude_effort and default_claude_effort not in CLAUDE_EFFORTS:
        raise SystemExit(
            "A2A_CLAUDE_EFFORT must be one of: " + ", ".join(CLAUDE_EFFORTS)
        )

    parser = argparse.ArgumentParser(
        description="Run a bounded Codex <-> Claude PR loop.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--repo",
        default=pathlib.Path("."),
        type=pathlib.Path,
        help="Target repo to operate on. Defaults to the current directory.",
    )
    parser.add_argument("--goal", help="Goal to plan and implement. Optional when --plan is passed.")
    parser.add_argument("--plan", type=pathlib.Path, help="Use an existing .plan.md file instead of creating one.")
    parser.add_argument("--base", default="main", help="Base branch for diff review and PR creation.")
    parser.add_argument("--branch", help="Branch to create/use. Defaults to a timestamped a2a/* branch.")
    parser.add_argument("--max-plan-rounds", type=int, default=2, help="Maximum plan negotiation rounds.")
    parser.add_argument("--max-rounds", type=int, default=3, help="Maximum implementation review rounds.")
    parser.add_argument("--skip-plan-review", action="store_true", help="Use the plan without implementer/reviewer negotiation.")
    parser.add_argument("--planner", choices=AGENTS, default="claude", help="Agent used to create new plans.")
    parser.add_argument("--implementer", choices=AGENTS, default="codex", help="Agent used to review plans, implement, and fix.")
    parser.add_argument("--reviewer", choices=AGENTS, default="claude", help="Agent used to approve plans and review implementations.")
    parser.add_argument(
        "--codex-model",
        default=default_codex_model,
        help="Model passed to codex exec. Can also be set with A2A_CODEX_MODEL.",
    )
    parser.add_argument(
        "--codex-effort",
        default=default_codex_effort,
        help="Codex reasoning effort config value. Can also be set with A2A_CODEX_EFFORT.",
    )
    parser.add_argument(
        "--claude-model",
        default=default_claude_model,
        help="Model passed to claude. Can also be set with A2A_CLAUDE_MODEL.",
    )
    parser.add_argument(
        "--claude-effort",
        choices=CLAUDE_EFFORTS,
        default=default_claude_effort,
        help="Optional effort passed to claude. Can also be set with A2A_CLAUDE_EFFORT.",
    )
    parser.add_argument("--gh-review", action="store_true", help="Use GitHub PR comments as the review surface.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--merge", action="store_true", help="Squash merge after reviewer approval.")
    args = parser.parse_args()
    args.codex_effort = normalize_codex_effort(args.codex_effort)

    repo = args.repo.expanduser().resolve()
    if not repo.exists():
        raise SystemExit(f"Repo does not exist: {repo}")
    if not args.goal and not args.plan:
        raise SystemExit("Either --goal or --plan is required.")

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S-%f")
    branch = args.branch or f"a2a/{stamp}"
    log_dir = pathlib.Path.cwd() / "a2a-logs" / stamp
    log = log_dir / "run.log"
    trace = WorkflowTrace(log)
    goal = args.goal
    if args.plan:
        plan_path = resolve_repo_path(repo, args.plan)
        if not plan_path.exists():
            raise SystemExit(f"Plan does not exist: {plan_path}")
        goal = goal or f"Execute plan {repo_relative(repo, plan_path)}"
    else:
        assert goal is not None
        plan_path = repo / ".a2a" / "plans" / f"{slugify_goal(goal)}.plan.md"

    ensure_a2a_dirs(repo, args.dry_run)
    trace.event(f"repo: {repo}")
    trace.event(f"branch setup: {branch}")
    ensure_branch(repo, branch, args.dry_run, log)

    plan = negotiate_plan(
        repo,
        goal,
        args.base,
        plan_path,
        args.planner,
        args.implementer,
        args.reviewer,
        args.max_plan_rounds,
        args.skip_plan_review,
        args.dry_run,
        log,
        trace,
        args.codex_model,
        args.codex_effort,
        args.claude_model,
        args.claude_effort,
        create_plan=args.plan is None,
    )

    run_agent(
        args.implementer,
        "implement approved plan",
        build_implement_prompt(goal, repo_relative(repo, plan_path), args.base),
        repo,
        args.dry_run,
        log,
        trace,
        args.codex_model,
        args.codex_effort,
        args.claude_model,
        args.claude_effort,
        artifact=repo_relative(repo, plan_path),
    )

    pr_url = ""
    if args.gh_review:
        trace.event(f"opening or updating PR before GitHub review: base {args.base}, branch {branch}")
        pr_url = open_or_update_pr(repo, args.base, branch, goal, plan, args.dry_run, log)
        approved = run_gh_review_loop(
            repo,
            goal,
            pr_url,
            args.reviewer,
            args.implementer,
            args.max_rounds,
            args.dry_run,
            log,
            trace,
            args.codex_model,
            args.codex_effort,
            args.claude_model,
            args.claude_effort,
        )
    else:
        approved = run_local_review_loop(
            repo,
            goal,
            args.base,
            plan_path,
            args.reviewer,
            args.implementer,
            args.max_rounds,
            args.dry_run,
            log,
            trace,
            args.codex_model,
            args.codex_effort,
            args.claude_model,
            args.claude_effort,
        )
        if approved:
            trace.event(f"opening or updating PR after local approval: base {args.base}, branch {branch}")
            pr_url = open_or_update_pr(repo, args.base, branch, goal, plan, args.dry_run, log)

    if not approved:
        pr_note = f" PR: {pr_url}" if pr_url else ""
        trace.event(f"not approved within {args.max_rounds} review rounds.{pr_note}")
        trace.event(f"logs: {log}")
        return 2

    trace.event(f"approved by {args.reviewer}. PR: {pr_url}")
    if args.merge:
        trace.event(f"merge requested: squash {pr_url}")
        gh_text(repo, ["pr", "merge", pr_url, "--squash", "--delete-branch"], args.dry_run, log)
        trace.event("squash merge requested")
    else:
        trace.event("merge skipped; pass --merge to squash merge automatically")
    trace.event(f"logs: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
