#!/usr/bin/env python3
"""
Headless Codex <-> Claude Code PR loop.

Claude plans and reviews. Codex critiques, implements, and fixes. Local git
state plus `.a2a/` files are the default coordination layer; GitHub PR review
is available with --gh-review. Start with --dry-run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
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


@dataclass
class CmdResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


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


def claude_print(prompt: str, repo: pathlib.Path, dry_run: bool, log: pathlib.Path) -> str:
    result = run(
        [
            "claude",
            "-p",
            "--permission-mode",
            "dontAsk",
            "--output-format",
            "text",
            prompt,
        ],
        cwd=repo,
        dry_run=dry_run,
        log_file=log,
    )
    require_ok(result, "Claude turn")
    return result.stdout


def codex_exec(prompt: str, repo: pathlib.Path, dry_run: bool, log: pathlib.Path) -> str:
    result = run(
        [
            "codex",
            "exec",
            "-C",
            str(repo),
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            prompt,
        ],
        cwd=repo,
        dry_run=dry_run,
        log_file=log,
    )
    require_ok(result, "Codex turn")
    return result.stdout


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

        ## Claude Plan

        {plan.strip()}

        ## Coordination

        This PR was opened by the Codex/Claude headless loop.
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
You are the planner in a Codex + Claude agent-to-agent workflow.

Target goal:
{goal}

Base branch: {base}

Write the implementation plan to:
{plan_path}

Include:
1. A concise implementation plan.
2. Acceptance criteria.
3. Tests/checks the implementer should run.
4. Risks or files likely to need attention.

Do not edit source files. End your response with PLAN_READY.
""".strip()


def build_plan_review_prompt(goal: str, base: str, plan_path: str) -> str:
    return f"""
You are Codex, reviewing Claude's plan before implementation.

Goal:
{goal}

Base branch: {base}

Read the plan at:
{plan_path}

Inspect the repo enough to catch missing steps, risky assumptions, weak tests,
or repo-specific implementation details. Update the plan file in place by
adding a "Codex Review" or "Codex Enhancements" section.

Do not implement the feature yet. End your response with PLAN_REVIEW_READY.
""".strip()


def build_plan_approval_prompt(goal: str, base: str, plan_path: str) -> str:
    return f"""
You are Claude, approving the implementation plan before Codex edits code.

Goal:
{goal}

Base branch: {base}

Review the enhanced plan at:
{plan_path}

If the plan is ready for implementation, end with exactly:
{PLAN_APPROVAL_TOKEN}

If changes are still needed, update the plan file in place with a concise
"Claude Follow-up" section and end with exactly:
{PLAN_CHANGES_TOKEN}

Do not implement the feature.
""".strip()


def build_implement_prompt(goal: str, plan_path: str, base: str) -> str:
    return f"""
You are Codex, the implementer in a two-agent workflow.

Goal:
{goal}

Plan file:
{plan_path}

Instructions:
- Read the plan file before editing.
- Inspect the repo before editing.
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
You are Claude, the reviewer/merge gate in a local-first Codex + Claude workflow.

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
You are Codex, the fixer in a local-first Codex + Claude workflow.

Goal:
{goal}

Plan file:
{plan_path}

Review file:
{review_path}

Instructions:
- Read the plan and review files.
- Inspect the local diff against `{base}`.
- Address all actionable review comments.
- Run relevant tests/checks.
- Commit fixes to the current branch.
- Do not push.
- Do not merge.
- End with IMPLEMENTATION_READY or explain the blocker.
""".strip()


def build_gh_review_prompt(goal: str, pr_url: str) -> str:
    return f"""
You are Claude, the reviewer/merge gate in a Codex + Claude workflow.

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
You are Codex, the fixer in a two-agent workflow.

Goal:
{goal}

PR:
{pr_url}

Claude's review:
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
    max_plan_rounds: int,
    dry_run: bool,
    log: pathlib.Path,
) -> str:
    plan_rel = repo_relative(repo, plan_path)
    print(f"[loop] writing plan: {plan_rel}")
    claude_print(build_plan_prompt(goal, base, plan_rel), repo, dry_run, log)

    for round_index in range(1, max_plan_rounds + 1):
        print(f"[loop] plan review round {round_index}/{max_plan_rounds}")
        codex_exec(build_plan_review_prompt(goal, base, plan_rel), repo, dry_run, log)
        approval = claude_print(build_plan_approval_prompt(goal, base, plan_rel), repo, dry_run, log)
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
    max_rounds: int,
    dry_run: bool,
    log: pathlib.Path,
) -> bool:
    plan_rel = repo_relative(repo, plan_path)
    for round_index in range(1, max_rounds + 1):
        review_path = repo / ".a2a" / "reviews" / f"review-{round_index}.md"
        review_rel = repo_relative(repo, review_path)
        print(f"[loop] local review round {round_index}/{max_rounds}: {review_rel}")
        review = claude_print(
            build_local_review_prompt(goal, base, plan_rel, review_rel),
            repo,
            dry_run,
            log,
        )
        if dry_run:
            review = APPROVAL_TOKEN
        if APPROVAL_TOKEN in review:
            return True

        codex_exec(build_local_fix_prompt(goal, base, plan_rel, review_rel), repo, dry_run, log)

    return False


def run_gh_review_loop(
    repo: pathlib.Path,
    goal: str,
    pr_url: str,
    max_rounds: int,
    dry_run: bool,
    log: pathlib.Path,
) -> bool:
    for round_index in range(1, max_rounds + 1):
        print(f"[loop] GitHub review round {round_index}/{max_rounds}")
        review = claude_print(build_gh_review_prompt(goal, pr_url), repo, dry_run, log)
        if dry_run:
            review = APPROVAL_TOKEN
        if APPROVAL_TOKEN in review:
            return True
        codex_exec(build_gh_fix_prompt(goal, pr_url, review), repo, dry_run, log)
        gh_text(
            repo,
            ["pr", "comment", pr_url, "--body", f"Codex pushed fixes for round {round_index}."],
            dry_run,
            log,
        )

    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a bounded Codex <-> Claude PR loop.")
    parser.add_argument(
        "--repo",
        default=pathlib.Path("."),
        type=pathlib.Path,
        help="Target repo to operate on. Defaults to the current directory.",
    )
    parser.add_argument("--goal", required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--branch")
    parser.add_argument("--max-plan-rounds", type=int, default=2)
    parser.add_argument("--max-rounds", type=int, default=3, help="Maximum implementation review rounds.")
    parser.add_argument("--gh-review", action="store_true", help="Use GitHub PR comments as the review surface.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--merge", action="store_true", help="Squash merge after Claude approval.")
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    if not repo.exists():
        raise SystemExit(f"Repo does not exist: {repo}")

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S-%f")
    branch = args.branch or f"a2a/{stamp}"
    log_dir = pathlib.Path.cwd() / "a2a-logs" / stamp
    log = log_dir / "run.log"
    plan_path = repo / ".a2a" / "plans" / f"{slugify_goal(args.goal)}.plan.md"

    ensure_a2a_dirs(repo, args.dry_run)
    ensure_branch(repo, branch, args.dry_run, log)

    plan = negotiate_plan(
        repo,
        args.goal,
        args.base,
        plan_path,
        args.max_plan_rounds,
        args.dry_run,
        log,
    )

    codex_exec(
        build_implement_prompt(args.goal, repo_relative(repo, plan_path), args.base),
        repo,
        args.dry_run,
        log,
    )

    pr_url = ""
    if args.gh_review:
        pr_url = open_or_update_pr(repo, args.base, branch, args.goal, plan, args.dry_run, log)
        approved = run_gh_review_loop(
            repo,
            args.goal,
            pr_url,
            args.max_rounds,
            args.dry_run,
            log,
        )
    else:
        approved = run_local_review_loop(
            repo,
            args.goal,
            args.base,
            plan_path,
            args.max_rounds,
            args.dry_run,
            log,
        )
        if approved:
            pr_url = open_or_update_pr(repo, args.base, branch, args.goal, plan, args.dry_run, log)

    if not approved:
        pr_note = f" PR: {pr_url}" if pr_url else ""
        print(f"[done] not approved within {args.max_rounds} review rounds.{pr_note}")
        print(f"[logs] {log}")
        return 2

    print(f"[done] approved by Claude. PR: {pr_url}")
    if args.merge:
        gh_text(repo, ["pr", "merge", pr_url, "--squash", "--delete-branch"], args.dry_run, log)
        print("[done] squash merge requested")
    else:
        print("[done] merge skipped; pass --merge to squash merge automatically")
    print(f"[logs] {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
