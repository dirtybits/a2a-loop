#!/usr/bin/env python3
"""
Headless Codex <-> Claude Code PR loop.

Claude plans and reviews. Codex implements and fixes. GitHub PR state is the
shared coordination layer. Start with --dry-run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shlex
import subprocess
import sys
import textwrap
from dataclasses import dataclass


APPROVAL_TOKEN = "MERGE_DECISION: APPROVE"


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


def open_or_update_pr(
    repo: pathlib.Path,
    base: str,
    branch: str,
    goal: str,
    plan: str,
    dry_run: bool,
    log: pathlib.Path,
) -> str:
    run(["git", "push", "-u", "origin", branch], cwd=repo, dry_run=dry_run, log_file=log)
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


def build_plan_prompt(goal: str, base: str) -> str:
    return f"""
You are the planner in a Codex + Claude agent-to-agent workflow.

Target goal:
{goal}

Base branch: {base}

Produce:
1. A concise implementation plan.
2. Acceptance criteria.
3. Tests/checks the implementer should run.
4. Risks or files likely to need attention.

Do not edit files. End with PLAN_READY.
""".strip()


def build_implement_prompt(goal: str, plan: str, base: str) -> str:
    return f"""
You are Codex, the implementer in a two-agent workflow.

Goal:
{goal}

Claude's plan:
{plan}

Instructions:
- Inspect the repo before editing.
- Implement the smallest complete change that satisfies the plan.
- Run relevant tests/checks.
- Commit your changes to the current branch.
- Do not merge.
- End your final response with IMPLEMENTATION_READY or explain the blocker.

Base branch: {base}
""".strip()


def build_review_prompt(goal: str, pr_url: str) -> str:
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


def build_fix_prompt(goal: str, pr_url: str, review: str) -> str:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a bounded Codex <-> Claude PR loop.")
    parser.add_argument("--repo", required=True, type=pathlib.Path)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--branch")
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--merge", action="store_true", help="Squash merge after Claude approval.")
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    if not repo.exists():
        raise SystemExit(f"Repo does not exist: {repo}")

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    branch = args.branch or f"a2a/{stamp}"
    log_dir = pathlib.Path.cwd() / "a2a-logs" / stamp
    log = log_dir / "run.log"

    ensure_branch(repo, branch, args.dry_run, log)

    plan = claude_print(build_plan_prompt(args.goal, args.base), repo, args.dry_run, log)
    if args.dry_run:
        plan = "DRY_RUN_PLAN"

    codex_exec(build_implement_prompt(args.goal, plan, args.base), repo, args.dry_run, log)
    pr_url = open_or_update_pr(repo, args.base, branch, args.goal, plan, args.dry_run, log)

    approved = False
    for round_index in range(1, args.max_rounds + 1):
        print(f"[loop] review round {round_index}/{args.max_rounds}")
        review = claude_print(build_review_prompt(args.goal, pr_url), repo, args.dry_run, log)
        if APPROVAL_TOKEN in review:
            approved = True
            break
        codex_exec(build_fix_prompt(args.goal, pr_url, review), repo, args.dry_run, log)
        gh_text(repo, ["pr", "comment", pr_url, "--body", f"Codex pushed fixes for round {round_index}."], args.dry_run, log)

    if not approved:
        print(f"[done] not approved within {args.max_rounds} rounds. PR: {pr_url}")
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

