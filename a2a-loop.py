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
import threading
import tomllib
from dataclasses import asdict, dataclass


APPROVAL_TOKEN = "MERGE_DECISION: APPROVE"
PLAN_APPROVAL_TOKEN = "PLAN_STATUS: approved"
# The loop only ever checks for the approval tokens. Any other reviewer
# output, including these changes-requested tokens, fails closed into
# another review round; the tokens exist so prompts can name an explicit
# alternative ending.
PLAN_CHANGES_TOKEN = "PLAN_STATUS: changes_requested"
REVIEW_CHANGES_TOKEN = "REVIEW_STATUS: changes_requested"
AGENTS = ("claude", "codex")
STATE_VERSION = 1
CODEX_EFFORTS = ("minimal", "low", "medium", "high")
CODEX_EFFORT_ALIASES = {
    "extra-high": "high",
    "xhigh": "high",
    "max": "high",
}
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_API_AUTH_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)
CLAUDE_DEFAULT_EFFORT = "high"
CODEX_CONFIG_SOURCE = "~/.codex/config.toml"
CLAUDE_SETTINGS_SOURCE = "~/.claude/settings.json"
AGENT_FATAL_PATTERNS = (
    "ERROR: unexpected status",
    "unexpected status 400 Bad Request",
    "requires a newer version of Codex",
)
DEFAULT_AGENT_TIMEOUT_SECONDS = 3600
# Fatal CLI errors surface on stderr or near the end of the transcript;
# scanning full stdout false-positives on diffs that merely quote them.
FATAL_SCAN_STDOUT_TAIL_LINES = 40


def warn(message: str) -> None:
    print(f"[a2a-loop] warning: {message}", file=sys.stderr)


def env_default(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value


def env_flag(name: str) -> bool:
    value = os.environ.get(name)
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def arg_was_passed(option: str) -> bool:
    return any(arg == option or arg.startswith(f"{option}=") for arg in sys.argv[1:])


def resolved_source(
    option: str,
    env_name: str,
    config_value: str | None,
    config_source: str,
    fallback_source: str | None = None,
) -> str:
    if arg_was_passed(option):
        return option
    if env_default(env_name):
        return env_name
    if config_value:
        return config_source
    return fallback_source or "unresolved"


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


def sanitize_display_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", value)
    cleaned = re.sub(r"\[[0-9;]*m\]?$", "", cleaned)
    cleaned = cleaned.strip()
    return cleaned or None


def read_codex_cli_defaults() -> tuple[str | None, str | None]:
    # Config files only feed defaults, so invalid values are dropped with a
    # warning instead of aborting; explicit flags/env still validate strictly.
    path = pathlib.Path.home() / ".codex" / "config.toml"
    if not path.exists():
        return None, None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None, None
    model = sanitize_display_value(data.get("model"))
    effort = sanitize_display_value(data.get("model_reasoning_effort"))
    if effort is not None:
        normalized = CODEX_EFFORT_ALIASES.get(effort, effort)
        if normalized not in CODEX_EFFORTS:
            warn(f"ignoring invalid model_reasoning_effort in {CODEX_CONFIG_SOURCE}: {effort}")
            effort = None
        else:
            effort = normalized
    return model, effort


def read_claude_cli_defaults() -> tuple[str | None, str | None]:
    path = pathlib.Path.home() / ".claude" / "settings.json"
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    model = sanitize_display_value(data.get("model"))
    effort = sanitize_display_value(data.get("effort"))
    if effort is not None and effort not in CLAUDE_EFFORTS:
        warn(f"ignoring invalid effort in {CLAUDE_SETTINGS_SOURCE}: {effort}")
        effort = None
    return model, effort


def ends_with_token(output: str, token: str, window: int = 5) -> bool:
    """True when one of the final non-empty lines is exactly `token`.

    The prompts require the token to be the ending of the response. A small
    window tolerates trailing CLI chrome (e.g. codex exec usage footers)
    while still rejecting prose that merely mentions the token mid-sentence.
    """
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    return token in lines[-window:]


def agent_timeout_seconds() -> int | None:
    value = env_default("A2A_AGENT_TIMEOUT_SECONDS")
    if value is None:
        return DEFAULT_AGENT_TIMEOUT_SECONDS
    try:
        seconds = int(value)
    except ValueError:
        raise SystemExit(f"A2A_AGENT_TIMEOUT_SECONDS must be an integer: {value}")
    return seconds if seconds > 0 else None


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

    def start_agent(
        self,
        agent: str,
        phase: str,
        model: str,
        effort: str,
        artifact: str | None = None,
    ) -> int:
        if self.active_agent and self.active_agent != agent:
            self.event(f"handoff: {self.active_agent} -> {agent}")
        self.active_agent = agent
        self.step += 1
        suffix = f" ({artifact})" if artifact else ""
        line = f"[agent:{agent}:{model}:{effort}] step {self.step} start: {phase}{suffix}"
        print(line)
        self._write(line)
        return self.step

    def finish_agent(self, agent: str, step: int, phase: str, model: str, effort: str, output: str) -> None:
        detail = f", output {len(output)} chars" if output else ""
        line = f"[agent:{agent}:{model}:{effort}] step {step} done: {phase}{detail}"
        print(line)
        self._write(line)

    def _write(self, line: str) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as f:
            f.write(f"{line}\n")


@dataclass
class RunState:
    version: int
    run_id: str
    repo: str
    branch: str
    base: str
    goal: str
    plan_path: str
    source_plan_path: str | None
    planner: str
    implementer: str
    reviewer: str
    max_plan_rounds: int
    max_rounds: int
    skip_plan_review: bool
    gh_review: bool
    merge: bool
    codex_model: str | None
    codex_effort: str | None
    codex_display_model: str
    codex_display_effort: str
    codex_model_source: str
    codex_effort_source: str
    claude_model: str | None
    claude_effort: str | None
    claude_display_model: str
    claude_display_effort: str
    claude_model_source: str
    claude_effort_source: str
    log_path: str
    claude_use_api_key: bool = False
    phase: str = "initialized"
    plan_review_round: int = 1
    local_review_round: int = 1
    gh_review_round: int = 1
    pr_url: str = ""
    approved: bool = False


def state_path(repo: pathlib.Path, run_id: str) -> pathlib.Path:
    return repo / ".a2a" / "runs" / run_id / "state.json"


def resolve_state_path(repo: pathlib.Path, value: str) -> pathlib.Path:
    candidate = pathlib.Path(value).expanduser()
    if candidate.is_dir():
        return candidate / "state.json"
    if candidate.suffix == ".json" or "/" in value:
        if not candidate.is_absolute():
            candidate = repo / candidate
        return candidate
    return state_path(repo, value)


def save_state(repo: pathlib.Path, state: RunState, dry_run: bool, trace: WorkflowTrace) -> None:
    if dry_run:
        return
    path = state_path(repo, state.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    trace.event(f"checkpoint saved: {repo_relative(repo, path)}")


def load_state(path: pathlib.Path) -> RunState:
    if not path.exists():
        raise SystemExit(f"Resume state does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != STATE_VERSION:
        raise SystemExit(f"Unsupported resume state version in {path}: {data.get('version')}")
    data.setdefault("source_plan_path", None)
    data.setdefault("codex_display_model", data.get("codex_model") or "unknown")
    data.setdefault("codex_display_effort", data.get("codex_effort") or "unknown")
    data.setdefault("codex_model_source", "legacy checkpoint")
    data.setdefault("codex_effort_source", "legacy checkpoint")
    data.setdefault("claude_display_model", data.get("claude_model") or "unknown")
    data.setdefault("claude_display_effort", data.get("claude_effort") or "unknown")
    data.setdefault("claude_model_source", "legacy checkpoint")
    data.setdefault("claude_effort_source", "legacy checkpoint")
    return RunState(**data)


def run(
    args: list[str],
    cwd: pathlib.Path,
    dry_run: bool,
    log_file: pathlib.Path,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> CmdResult:
    rendered = " ".join(shlex.quote(a) for a in args)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"\n\n$ {rendered}\n")

    if dry_run:
        print(f"[dry-run] {rendered}")
        with log_file.open("a", encoding="utf-8") as f:
            f.write("[dry-run] skipped\n")
        return CmdResult(args=args, returncode=0, stdout="", stderr="")

    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def pump_stdout() -> None:
        # Stream stdout into the run log as it arrives so long agent turns
        # stay observable via tail -f instead of appearing all at once.
        assert proc.stdout is not None
        with log_file.open("a", encoding="utf-8") as f:
            for line in proc.stdout:
                stdout_lines.append(line)
                f.write(line)
                f.flush()

    def pump_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)

    pumps = [
        threading.Thread(target=pump_stdout, daemon=True),
        threading.Thread(target=pump_stderr, daemon=True),
    ]
    for pump in pumps:
        pump.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    for pump in pumps:
        pump.join()

    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
    with log_file.open("a", encoding="utf-8") as f:
        if stderr:
            f.write("\n[stderr]\n")
            f.write(stderr)
    result = CmdResult(args=args, returncode=proc.returncode, stdout=stdout, stderr=stderr)
    if timed_out:
        raise SystemExit(format_command_failure("Command", result, f"timed out after {timeout}s"))
    return result


def require_ok(result: CmdResult, context: str) -> None:
    if result.returncode != 0:
        raise SystemExit(format_command_failure(context, result, f"exit code {result.returncode}"))


def format_command_failure(context: str, result: CmdResult, reason: str) -> str:
    rendered = " ".join(shlex.quote(a) for a in result.args)
    parts = [
        f"{context} failed ({reason}): {rendered}",
    ]
    if result.stdout.strip():
        parts.extend(["", "[stdout]", result.stdout.strip()])
    if result.stderr.strip():
        parts.extend(["", "[stderr]", result.stderr.strip()])
    return "\n".join(parts)


def require_no_agent_error(result: CmdResult, context: str) -> None:
    stdout_tail = "\n".join(result.stdout.splitlines()[-FATAL_SCAN_STDOUT_TAIL_LINES:])
    combined = "\n".join(part for part in (stdout_tail, result.stderr) if part)
    for pattern in AGENT_FATAL_PATTERNS:
        if pattern in combined:
            raise SystemExit(format_command_failure(context, result, f"fatal agent output: {pattern}"))


def claude_env(use_api_key: bool) -> dict[str, str] | None:
    if use_api_key:
        return None
    env = os.environ.copy()
    for name in CLAUDE_API_AUTH_ENV_VARS:
        env.pop(name, None)
    return env


def claude_print(
    prompt: str,
    repo: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
    model: str | None,
    effort: str | None,
    use_api_key: bool,
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
        env=claude_env(use_api_key),
        timeout=agent_timeout_seconds(),
    )
    require_ok(result, "Claude turn")
    require_no_agent_error(result, "Claude turn")
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
        timeout=agent_timeout_seconds(),
    )
    require_ok(result, "Codex turn")
    require_no_agent_error(result, "Codex turn")
    return result.stdout


def run_agent(
    agent: str,
    phase: str,
    prompt: str,
    repo: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
    trace: WorkflowTrace,
    state: RunState,
    artifact: str | None = None,
) -> str:
    if agent == "claude":
        model = state.claude_display_model
        effort = state.claude_display_effort
        step = trace.start_agent(agent, phase, model, effort, artifact)
        output = claude_print(
            prompt, repo, dry_run, log, state.claude_model, state.claude_effort, state.claude_use_api_key
        )
        trace.finish_agent(agent, step, phase, model, effort, output)
        return output
    if agent == "codex":
        model = state.codex_display_model
        effort = state.codex_display_effort
        step = trace.start_agent(agent, phase, model, effort, artifact)
        output = codex_exec(prompt, repo, dry_run, log, state.codex_model, state.codex_effort)
        trace.finish_agent(agent, step, phase, model, effort, output)
        return output
    raise ValueError(f"Unsupported agent: {agent}")


def gh_json(repo: pathlib.Path, args: list[str], dry_run: bool, log: pathlib.Path) -> list | dict:
    result = run(["gh", *args], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(result, "gh command")
    if dry_run or not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def gh_text(repo: pathlib.Path, args: list[str], dry_run: bool, log: pathlib.Path) -> str:
    result = run(["gh", *args], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(result, "gh command")
    return result.stdout


def ensure_branch(repo: pathlib.Path, branch: str, dry_run: bool, log: pathlib.Path) -> None:
    result = run(["git", "checkout", "-B", branch], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(result, "branch setup")


def checkout_branch(repo: pathlib.Path, branch: str, dry_run: bool, log: pathlib.Path) -> None:
    result = run(["git", "checkout", branch], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(result, "branch checkout")


def slugify_goal(goal: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")
    return slug[:64].strip("-") or "a2a-plan"


def repo_relative(repo: pathlib.Path, path: pathlib.Path) -> str:
    return path.relative_to(repo).as_posix()


def display_path(repo: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return repo_relative(repo, path.resolve())
    except ValueError:
        return str(path)


def trace_run_defaults(repo: pathlib.Path, plan_path: pathlib.Path, state: RunState, trace: WorkflowTrace) -> None:
    review_mode = "GitHub PR comments" if state.gh_review else "local review files"
    trace.event(
        "defaults: "
        f"base={state.base}, planner={state.planner}, implementer={state.implementer}, "
        f"reviewer={state.reviewer}, review={review_mode}, "
        f"max-plan-rounds={state.max_plan_rounds}, max-rounds={state.max_rounds}"
    )
    trace.event(
        "Codex defaults: "
        f"model={state.codex_display_model} ({state.codex_model_source}), "
        f"effort={state.codex_display_effort} ({state.codex_effort_source})"
    )
    trace.event(
        "Claude defaults: "
        f"model={state.claude_display_model} ({state.claude_model_source}), "
        f"effort={state.claude_display_effort} ({state.claude_effort_source})"
    )
    trace.event(
        "artifacts: "
        f"plan={display_path(repo, plan_path)}, "
        f"state={display_path(repo, state_path(repo, state.run_id))}, "
        f"log={display_path(repo, pathlib.Path(state.log_path))}"
    )
    if state.source_plan_path and state.source_plan_path != state.plan_path:
        trace.event(f"plan ledger source: {state.source_plan_path}")


def plan_is_in_a2a(repo: pathlib.Path, path: pathlib.Path) -> bool:
    return display_path(repo, path).startswith(".a2a/")


def materialize_working_plan(
    repo: pathlib.Path,
    source_path: pathlib.Path,
    run_id: str,
    dry_run: bool,
    trace: WorkflowTrace,
) -> pathlib.Path:
    if plan_is_in_a2a(repo, source_path):
        return source_path
    dest = repo / ".a2a" / "plans" / f"{run_id}-{source_path.name}"
    if dry_run:
        trace.event(
            f"dry-run would copy plan ledger for writable run state: "
            f"{display_path(repo, source_path)} -> {display_path(repo, dest)}"
        )
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    trace.event(
        f"copied plan ledger for writable run state: "
        f"{display_path(repo, source_path)} -> {display_path(repo, dest)}"
    )
    return dest


def ensure_a2a_dirs(repo: pathlib.Path, dry_run: bool) -> None:
    if dry_run:
        return
    (repo / ".a2a" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / ".a2a" / "reviews").mkdir(parents=True, exist_ok=True)
    (repo / ".a2a" / "runs").mkdir(parents=True, exist_ok=True)
    (repo / ".a2a" / "logs").mkdir(parents=True, exist_ok=True)


def ensure_gitignore(repo: pathlib.Path, dry_run: bool, trace: WorkflowTrace | None = None) -> None:
    patterns = (".a2a/", "a2a-logs/")
    gitignore = repo / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    normalized = {
        line.strip().rstrip("/")
        for line in existing.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = [pattern for pattern in patterns if pattern.rstrip("/") not in normalized]
    if not missing:
        return
    if dry_run:
        if trace:
            trace.event("dry-run would update .gitignore: " + ", ".join(missing))
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    suffix = "".join(f"{pattern}\n" for pattern in missing)
    gitignore.write_text(existing + prefix + suffix, encoding="utf-8")
    if trace:
        trace.event("updated .gitignore: " + ", ".join(missing))


def read_if_present(path: pathlib.Path, fallback: str = "") -> str:
    if not path.exists():
        return fallback
    return path.read_text(encoding="utf-8")


def persist_review_output(path: pathlib.Path, output: str, dry_run: bool, trace: WorkflowTrace) -> None:
    if not output.strip():
        trace.event(f"review output empty; no fallback write for {path.name}")
        return
    if path.exists() and path.read_text(encoding="utf-8").strip():
        trace.event(f"review file already written: {path.name}")
        return
    if dry_run:
        trace.event(f"dry-run would persist reviewer stdout: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(output.rstrip() + "\n", encoding="utf-8")
    trace.event(f"persisted reviewer stdout: {path.name}")


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
        url = existing[0].get("url")
        if not url:
            raise SystemExit(f"Unexpected gh pr list output for branch {branch}: {existing!r}")
        return url

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
    plan_path: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
    trace: WorkflowTrace,
    state: RunState,
    create_plan: bool,
) -> str:
    plan_rel = repo_relative(repo, plan_path)
    if state.phase == "initialized":
        if create_plan:
            trace.event(f"planning starts: {state.planner} writes {plan_rel}")
            run_agent(
                state.planner,
                "write implementation plan",
                build_plan_prompt(state.goal, state.base, plan_rel),
                repo,
                dry_run,
                log,
                trace,
                state,
                artifact=plan_rel,
            )
        else:
            trace.event(f"using existing plan: {plan_rel}")
        state.phase = "plan_written"
        save_state(repo, state, dry_run, trace)
    elif state.phase == "plan_written":
        trace.event(f"resuming plan review from: {plan_rel}")
    else:
        trace.event(f"plan already ready: {plan_rel}")

    if state.phase != "plan_written":
        return read_if_present(plan_path, "DRY_RUN_PLAN")

    if state.skip_plan_review:
        trace.event("plan review skipped")
        state.phase = "plan_ready"
        save_state(repo, state, dry_run, trace)
        return read_if_present(plan_path, "DRY_RUN_PLAN")

    for round_index in range(state.plan_review_round, state.max_plan_rounds + 1):
        trace.event(f"plan review round {round_index}/{state.max_plan_rounds}")
        run_agent(
            state.implementer,
            "review and enhance plan",
            build_plan_review_prompt(state.goal, state.base, plan_rel),
            repo,
            dry_run,
            log,
            trace,
            state,
            artifact=plan_rel,
        )
        approval = run_agent(
            state.reviewer,
            "approve plan",
            build_plan_approval_prompt(state.goal, state.base, plan_rel),
            repo,
            dry_run,
            log,
            trace,
            state,
            artifact=plan_rel,
        )
        if dry_run:
            approval = PLAN_APPROVAL_TOKEN
        if ends_with_token(approval, PLAN_APPROVAL_TOKEN):
            state.phase = "plan_ready"
            save_state(repo, state, dry_run, trace)
            return read_if_present(plan_path, "DRY_RUN_PLAN")
        state.plan_review_round = round_index + 1
        save_state(repo, state, dry_run, trace)

    raise SystemExit(
        f"Plan was not approved within {state.max_plan_rounds} rounds. "
        f"Resume with more rounds: a2a-loop --resume {state.run_id} --max-plan-rounds 2"
    )


def run_local_review_loop(
    repo: pathlib.Path,
    plan_path: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
    trace: WorkflowTrace,
    state: RunState,
) -> bool:
    plan_rel = repo_relative(repo, plan_path)
    # Reviews are namespaced by run id so a stale review-N.md from an earlier
    # run can never be mistaken for this run's review.
    review_dir = repo / ".a2a" / "reviews" / state.run_id
    if not dry_run:
        review_dir.mkdir(parents=True, exist_ok=True)
    for round_index in range(state.local_review_round, state.max_rounds + 1):
        review_path = review_dir / f"review-{round_index}.md"
        review_rel = repo_relative(repo, review_path)
        trace.event(f"local review round {round_index}/{state.max_rounds}: {review_rel}")
        review = run_agent(
            state.reviewer,
            "review local diff",
            build_local_review_prompt(state.goal, state.base, plan_rel, review_rel),
            repo,
            dry_run,
            log,
            trace,
            state,
            artifact=review_rel,
        )
        persist_review_output(review_path, review, dry_run, trace)
        if dry_run:
            review = APPROVAL_TOKEN
        if ends_with_token(review, APPROVAL_TOKEN):
            trace.event(f"review approved by {state.reviewer}")
            state.phase = "approved"
            state.approved = True
            save_state(repo, state, dry_run, trace)
            return True

        run_agent(
            state.implementer,
            "fix local review comments",
            build_local_fix_prompt(state.goal, state.base, plan_rel, review_rel),
            repo,
            dry_run,
            log,
            trace,
            state,
            artifact=review_rel,
        )
        state.phase = "implementation_ready"
        state.local_review_round = round_index + 1
        save_state(repo, state, dry_run, trace)

    return False


def run_gh_review_loop(
    repo: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
    trace: WorkflowTrace,
    state: RunState,
) -> bool:
    for round_index in range(state.gh_review_round, state.max_rounds + 1):
        trace.event(f"GitHub review round {round_index}/{state.max_rounds}: {state.pr_url}")
        review = run_agent(
            state.reviewer,
            "review GitHub PR",
            build_gh_review_prompt(state.goal, state.pr_url),
            repo,
            dry_run,
            log,
            trace,
            state,
            artifact=state.pr_url,
        )
        if dry_run:
            review = APPROVAL_TOKEN
        if ends_with_token(review, APPROVAL_TOKEN):
            trace.event(f"GitHub review approved by {state.reviewer}")
            state.phase = "approved"
            state.approved = True
            save_state(repo, state, dry_run, trace)
            return True
        run_agent(
            state.implementer,
            "fix GitHub review comments",
            build_gh_fix_prompt(state.goal, state.pr_url, review),
            repo,
            dry_run,
            log,
            trace,
            state,
            artifact=state.pr_url,
        )
        gh_text(
            repo,
            ["pr", "comment", state.pr_url, "--body", f"Implementer pushed fixes for round {round_index}."],
            dry_run,
            log,
        )
        state.phase = "pr_ready"
        state.gh_review_round = round_index + 1
        save_state(repo, state, dry_run, trace)

    return False


def main() -> int:
    codex_cli_model, codex_cli_effort = read_codex_cli_defaults()
    claude_cli_model, claude_cli_effort = read_claude_cli_defaults()
    default_codex_model = env_default("A2A_CODEX_MODEL") or codex_cli_model
    default_codex_effort = env_default("A2A_CODEX_EFFORT") or codex_cli_effort
    default_claude_model = env_default("A2A_CLAUDE_MODEL") or claude_cli_model
    default_claude_effort = env_default("A2A_CLAUDE_EFFORT") or claude_cli_effort or CLAUDE_DEFAULT_EFFORT
    default_claude_use_api_key = env_flag("A2A_CLAUDE_USE_API_KEY")

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
    parser.add_argument("--resume", help="Resume a checkpoint from .a2a/runs/<id>/state.json, or pass a state path.")
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
    parser.add_argument(
        "--claude-use-api-key",
        action="store_true",
        default=default_claude_use_api_key,
        help="Let claude inherit ANTHROPIC_* API-key auth instead of using claude.ai login/subscription auth.",
    )
    args = parser.parse_args()
    args.codex_effort = normalize_codex_effort(args.codex_effort)
    # argparse choices do not validate defaults, so an effort injected via
    # A2A_CLAUDE_EFFORT is checked here on the resolved value.
    if args.claude_effort and args.claude_effort not in CLAUDE_EFFORTS:
        raise SystemExit("Claude effort must be one of: " + ", ".join(CLAUDE_EFFORTS))
    codex_display_model = args.codex_model or "unknown"
    codex_display_effort = args.codex_effort or "unknown"
    claude_display_model = args.claude_model or "unknown"
    claude_display_effort = args.claude_effort or "unknown"
    codex_model_source = resolved_source(
        "--codex-model",
        "A2A_CODEX_MODEL",
        codex_cli_model,
        CODEX_CONFIG_SOURCE,
    )
    codex_effort_source = resolved_source(
        "--codex-effort",
        "A2A_CODEX_EFFORT",
        codex_cli_effort,
        CODEX_CONFIG_SOURCE,
    )
    claude_model_source = resolved_source(
        "--claude-model",
        "A2A_CLAUDE_MODEL",
        claude_cli_model,
        CLAUDE_SETTINGS_SOURCE,
    )
    claude_effort_source = resolved_source(
        "--claude-effort",
        "A2A_CLAUDE_EFFORT",
        claude_cli_effort,
        CLAUDE_SETTINGS_SOURCE,
        "coordinator default",
    )

    initial_repo = args.repo.expanduser().resolve()
    if not initial_repo.exists():
        raise SystemExit(f"Repo does not exist: {initial_repo}")

    if args.resume:
        loaded_path = resolve_state_path(initial_repo, args.resume).resolve()
        state = load_state(loaded_path)
        repo = pathlib.Path(state.repo).expanduser().resolve()
        if not repo.exists():
            raise SystemExit(f"Repo from resume state does not exist: {repo}")
        log = pathlib.Path(state.log_path).expanduser()
        trace = WorkflowTrace(log)
        extra_plan_rounds = args.max_plan_rounds
        extra_review_rounds = args.max_rounds
        state.max_plan_rounds = max(
            state.max_plan_rounds,
            state.plan_review_round + extra_plan_rounds - 1,
        )
        state.max_rounds = max(
            state.max_rounds,
            state.local_review_round + extra_review_rounds - 1,
            state.gh_review_round + extra_review_rounds - 1,
        )
        if args.merge:
            state.merge = True
        # Explicitly passed flags override the checkpoint; defaults resolved
        # from config files do not, so a resume keeps the run's settings.
        overrides: list[str] = []
        if arg_was_passed("--planner"):
            state.planner = args.planner
            overrides.append(f"planner={args.planner}")
        if arg_was_passed("--implementer"):
            state.implementer = args.implementer
            overrides.append(f"implementer={args.implementer}")
        if arg_was_passed("--reviewer"):
            state.reviewer = args.reviewer
            overrides.append(f"reviewer={args.reviewer}")
        if arg_was_passed("--codex-model"):
            state.codex_model = args.codex_model
            state.codex_display_model = args.codex_model or "unknown"
            state.codex_model_source = "--codex-model"
            overrides.append(f"codex-model={state.codex_display_model}")
        if arg_was_passed("--codex-effort"):
            state.codex_effort = args.codex_effort
            state.codex_display_effort = args.codex_effort or "unknown"
            state.codex_effort_source = "--codex-effort"
            overrides.append(f"codex-effort={state.codex_display_effort}")
        if arg_was_passed("--claude-model"):
            state.claude_model = args.claude_model
            state.claude_display_model = args.claude_model or "unknown"
            state.claude_model_source = "--claude-model"
            overrides.append(f"claude-model={state.claude_display_model}")
        if arg_was_passed("--claude-effort"):
            state.claude_effort = args.claude_effort
            state.claude_display_effort = args.claude_effort or "unknown"
            state.claude_effort_source = "--claude-effort"
            overrides.append(f"claude-effort={state.claude_display_effort}")
        if arg_was_passed("--claude-use-api-key"):
            state.claude_use_api_key = True
            overrides.append("claude-use-api-key=true")
        plan_path = resolve_repo_path(repo, pathlib.Path(state.plan_path))
        create_plan = False
        trace.event(f"resuming run {state.run_id} from {repo_relative(repo, loaded_path)}")
        for override in overrides:
            trace.event(f"resume override: {override}")
    else:
        repo = initial_repo
        if not args.goal and not args.plan:
            raise SystemExit("Either --goal, --plan, or --resume is required.")

        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S-%f")
        branch = args.branch or f"a2a/{stamp}"
        log_dir = repo / ".a2a" / "logs" / stamp
        log = log_dir / "run.log"
        trace = WorkflowTrace(log)
        goal = args.goal
        source_plan_path = None
        if args.plan:
            plan_path = resolve_repo_path(repo, args.plan)
            if not plan_path.exists():
                raise SystemExit(f"Plan does not exist: {plan_path}")
            goal = goal or f"Execute plan {repo_relative(repo, plan_path)}"
            source_plan_path = repo_relative(repo, plan_path)
            plan_path = materialize_working_plan(repo, plan_path, stamp, args.dry_run, trace)
            create_plan = False
        else:
            assert goal is not None
            # Namespaced by run id so runs with similar goals never share a
            # plan ledger.
            plan_path = repo / ".a2a" / "plans" / f"{stamp}-{slugify_goal(goal)}.plan.md"
            create_plan = True
        state = RunState(
            version=STATE_VERSION,
            run_id=stamp,
            repo=str(repo),
            branch=branch,
            base=args.base,
            goal=goal,
            plan_path=repo_relative(repo, plan_path),
            source_plan_path=source_plan_path,
            planner=args.planner,
            implementer=args.implementer,
            reviewer=args.reviewer,
            max_plan_rounds=args.max_plan_rounds,
            max_rounds=args.max_rounds,
            skip_plan_review=args.skip_plan_review,
            gh_review=args.gh_review,
            merge=args.merge,
            codex_model=args.codex_model,
            codex_effort=args.codex_effort,
            codex_display_model=codex_display_model,
            codex_display_effort=codex_display_effort,
            codex_model_source=codex_model_source,
            codex_effort_source=codex_effort_source,
            claude_model=args.claude_model,
            claude_effort=args.claude_effort,
            claude_display_model=claude_display_model,
            claude_display_effort=claude_display_effort,
            claude_model_source=claude_model_source,
            claude_effort_source=claude_effort_source,
            claude_use_api_key=args.claude_use_api_key,
            log_path=str(log),
        )

    ensure_a2a_dirs(repo, args.dry_run)
    trace.event(f"repo: {repo}")
    trace.event(
        "Claude auth: "
        + ("API key env inherited" if state.claude_use_api_key else "claude.ai login/subscription")
    )
    trace_run_defaults(repo, plan_path, state, trace)
    ensure_gitignore(repo, args.dry_run, trace)
    if args.resume:
        trace.event(f"branch checkout: {state.branch}")
        checkout_branch(repo, state.branch, args.dry_run, log)
    else:
        trace.event(f"branch setup: {state.branch}")
        ensure_branch(repo, state.branch, args.dry_run, log)
        save_state(repo, state, args.dry_run, trace)

    plan = negotiate_plan(
        repo,
        plan_path,
        args.dry_run,
        log,
        trace,
        state,
        create_plan=create_plan,
    )

    if state.phase == "plan_ready":
        run_agent(
            state.implementer,
            "implement approved plan",
            build_implement_prompt(state.goal, repo_relative(repo, plan_path), state.base),
            repo,
            args.dry_run,
            log,
            trace,
            state,
            artifact=repo_relative(repo, plan_path),
        )
        state.phase = "implementation_ready"
        save_state(repo, state, args.dry_run, trace)
    elif state.phase == "implementation_ready":
        trace.event("implementation already ready; resuming review")
    elif state.phase in ("approved", "pr_ready", "done"):
        trace.event(f"implementation/review already reached phase: {state.phase}")
    else:
        raise SystemExit(f"Cannot continue from phase {state.phase}")

    if state.phase == "done":
        trace.event(f"run already complete. PR: {state.pr_url}")
        trace.event(f"logs: {log}")
        return 0

    if state.gh_review:
        if not state.pr_url:
            trace.event(f"opening or updating PR before GitHub review: base {state.base}, branch {state.branch}")
            state.pr_url = open_or_update_pr(repo, state.base, state.branch, state.goal, plan, args.dry_run, log)
            state.phase = "pr_ready"
            save_state(repo, state, args.dry_run, trace)
        approved = state.phase == "approved" or run_gh_review_loop(
            repo,
            args.dry_run,
            log,
            trace,
            state,
        )
    else:
        approved = state.phase == "approved" or run_local_review_loop(
            repo,
            plan_path,
            args.dry_run,
            log,
            trace,
            state,
        )
        if approved:
            if not state.pr_url:
                trace.event(f"opening or updating PR after local approval: base {state.base}, branch {state.branch}")
                state.pr_url = open_or_update_pr(repo, state.base, state.branch, state.goal, plan, args.dry_run, log)
                save_state(repo, state, args.dry_run, trace)

    if not approved:
        pr_note = f" PR: {state.pr_url}" if state.pr_url else ""
        trace.event(f"not approved within {state.max_rounds} review rounds.{pr_note}")
        trace.event(f"resume with: a2a-loop --resume {state.run_id}")
        trace.event(f"logs: {log}")
        return 2

    trace.event(f"approved by {state.reviewer}. PR: {state.pr_url}")
    if state.merge:
        trace.event(f"merge requested: squash {state.pr_url}")
        gh_text(repo, ["pr", "merge", state.pr_url, "--squash", "--delete-branch"], args.dry_run, log)
        trace.event("squash merge requested")
    else:
        trace.event("merge skipped; pass --merge to squash merge automatically")
    state.phase = "done"
    save_state(repo, state, args.dry_run, trace)
    trace.event(f"logs: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
