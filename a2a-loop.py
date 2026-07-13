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
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
from dataclasses import asdict, dataclass
from typing import Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 compatibility
    tomllib = None


APPROVAL_TOKEN = "MERGE_DECISION: APPROVE"
PLAN_READY_TOKEN = "PLAN_READY"
PLAN_REVIEW_READY_TOKEN = "PLAN_REVIEW_READY"
PLAN_APPROVAL_TOKEN = "PLAN_STATUS: approved"
PLAN_BLOCKED_TOKEN = "PLAN_STATUS: blocked"
# The loop only ever checks for the approval tokens. Any other reviewer
# output, including these changes-requested tokens, fails closed into
# another review round; the tokens exist so prompts can name an explicit
# alternative ending.
PLAN_CHANGES_TOKEN = "PLAN_STATUS: changes_requested"
REVIEW_CHANGES_TOKEN = "REVIEW_STATUS: changes_requested"
PLAN_UPDATE_BEGIN = "A2A_PLAN_UPDATE_BEGIN"
PLAN_UPDATE_END = "A2A_PLAN_UPDATE_END"
PLAN_APPEND_BEGIN = "A2A_PLAN_APPEND_BEGIN"
PLAN_APPEND_END = "A2A_PLAN_APPEND_END"
COMMIT_MESSAGE_BEGIN = "A2A_COMMIT_MESSAGE_BEGIN"
COMMIT_MESSAGE_END = "A2A_COMMIT_MESSAGE_END"
DECISION_REASON_PREFIX = "A2A_REASON:"
IMPLEMENTATION_READY_TOKEN = "IMPLEMENTATION_READY"
IMPLEMENTATION_BLOCKED_TOKEN = "IMPLEMENTATION_STATUS: blocked"
LATEST_RESUME = "__latest__"
AGENTS = ("claude", "codex")
STATE_VERSION = 1
# Codex CLI 0.144.0 exposes expanded reasoning tiers for GPT-5.6 models.
# Keep `minimal` for older Codex models that still accept it.
CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
CODEX_EFFORT_ALIASES = {
    "extra-high": "xhigh",
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
# Plan notes carrying these markers are a control channel from the human to
# the agents: echoed into prompts and the PR body, protected by the
# append-only plan rule.
CONSTRAINT_MARKERS = ("SEQUENCING", "DECISION", "stop-the-line", "founder-acked")
# The closeout section of the plan must carry all four labels before the
# coordinator will open a PR; this keeps verification claims honest.
CLOSEOUT_REQUIRED_LABELS = (
    "Verified:",
    "Attempted-blocked",
    "Deferred",
    "Not claimed:",
)
CONVENTION_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    ".github/copilot-instructions.md",
)
VERBOSE_LINE_LIMIT = 240
CLAUDE_REVIEW_ALLOWED_TOOLS = (
    "Read",
    "Glob",
    "Grep",
    "Bash(git status*)",
    "Bash(git log*)",
    "Bash(git diff*)",
    "Bash(git show*)",
    "Bash(git branch*)",
    "Bash(git rev-parse*)",
    "Bash(git merge-base*)",
    "Bash(git ls-files*)",
    "Bash(forge test*)",
    "Bash(forge build*)",
    "Bash(npm run *)",
    "Bash(npm test*)",
    "Bash(pnpm *)",
    "Bash(bun *)",
    "Bash(yarn *)",
    "Bash(cargo test*)",
    "Bash(go test*)",
    "Bash(pytest *)",
    "Bash(python* -m pytest*)",
    "Bash(gh pr list*)",
    "Bash(gh pr view*)",
    "Bash(gh pr checks*)",
)
CLAUDE_REVIEW_PHASES = {
    "approve plan",
    "review local diff",
    "review GitHub PR",
}


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
            + ". GPT-5.6 supports xhigh, max, and ultra; extra-high maps to xhigh."
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
    if tomllib is None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None, None
        model = read_simple_toml_string(raw, "model")
        effort = read_simple_toml_string(raw, "model_reasoning_effort")
        return model, normalize_config_codex_effort(effort)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None, None
    model = sanitize_display_value(data.get("model"))
    effort = sanitize_display_value(data.get("model_reasoning_effort"))
    return model, normalize_config_codex_effort(effort)


def read_simple_toml_string(raw: str, key: str) -> str | None:
    pattern = rf"(?m)^\s*{re.escape(key)}\s*=\s*(['\"])(.*?)\1\s*(?:#.*)?$"
    match = re.search(pattern, raw)
    if not match:
        return None
    return sanitize_display_value(match.group(2))


def normalize_config_codex_effort(effort: str | None) -> str | None:
    if effort is not None:
        normalized = CODEX_EFFORT_ALIASES.get(effort, effort)
        if normalized not in CODEX_EFFORTS:
            warn(f"ignoring invalid model_reasoning_effort in {CODEX_CONFIG_SOURCE}: {effort}")
            return None
        return normalized
    return None


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


def format_codex_auth_status(output: str, returncode: int) -> str:
    lines = [
        sanitize_display_value(line)
        for line in output.splitlines()
        if sanitize_display_value(line)
    ]
    lines = [line for line in lines if not line.startswith("WARNING:")]
    if lines:
        return "; ".join(lines)
    return f"unknown; `codex login status` exited {returncode}"


def codex_auth_status(repo: pathlib.Path, dry_run: bool, log: pathlib.Path) -> str:
    if dry_run:
        return "not checked in dry-run; run `codex login status` to inspect local login"
    result = run(
        ["codex", "login", "status"],
        cwd=repo,
        dry_run=False,
        log_file=log,
        timeout=30,
        stream_output=False,
    )
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return format_codex_auth_status(combined, result.returncode)


def format_claude_auth_status(data: dict[str, object]) -> str:
    logged_in = data.get("loggedIn")
    auth_method = sanitize_display_value(data.get("authMethod")) or "unknown"
    api_provider = sanitize_display_value(data.get("apiProvider"))
    account = (
        sanitize_display_value(data.get("email"))
        or sanitize_display_value(data.get("accountEmail"))
        or sanitize_display_value(data.get("username"))
        or sanitize_display_value(data.get("account"))
    )
    status = "logged in" if logged_in is True else "not logged in" if logged_in is False else "unknown"
    parts = [status, f"method={auth_method}"]
    if account:
        parts.append(f"account={account}")
    if api_provider:
        parts.append(f"provider={api_provider}")
    return ", ".join(parts)


def claude_auth_status(repo: pathlib.Path, dry_run: bool, log: pathlib.Path) -> str:
    if dry_run:
        return "not checked in dry-run; run `claude auth status` to inspect local login"
    result = run(
        ["claude", "auth", "status", "--json"],
        cwd=repo,
        dry_run=False,
        log_file=log,
        timeout=30,
        stream_output=False,
    )
    if result.stdout.strip():
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "unknown; `claude auth status --json` returned non-JSON output"
        if isinstance(data, dict):
            return format_claude_auth_status(data)
    if result.stderr.strip():
        return f"unknown; `claude auth status` exited {result.returncode}: {result.stderr.strip()}"
    return f"unknown; `claude auth status` exited {result.returncode}"


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
    decision_log_path: str
    claude_use_api_key: bool = False
    phase: str = "initialized"
    plan_review_round: int = 1
    local_review_round: int = 1
    gh_review_round: int = 1
    pr_url: str = ""
    approved: bool = False
    verbose: bool = False
    final_review_path: str = ""
    blocked_reason: str = ""
    blocked_resume_phase: str = ""
    pending_review_path: str = ""
    pending_review_round: int = 0
    source_plan_sha256: str = ""


def state_path(repo: pathlib.Path, run_id: str) -> pathlib.Path:
    return repo / ".a2a" / "runs" / run_id / "state.json"


def decision_log_path(repo: pathlib.Path, run_id: str) -> pathlib.Path:
    return repo / ".a2a" / "runs" / run_id / "decisions.md"


def latest_state_path(repo: pathlib.Path) -> pathlib.Path:
    runs_dir = repo / ".a2a" / "runs"
    if not runs_dir.exists():
        raise SystemExit(f"No runs directory exists yet: {runs_dir}")
    candidates = [path / "state.json" for path in runs_dir.iterdir() if path.is_dir()]
    candidates = [path for path in candidates if path.exists()]
    if not candidates:
        raise SystemExit(f"No resume checkpoints found under: {runs_dir}")
    return max(candidates, key=lambda path: path.parent.stat().st_mtime)


def resolve_state_path(repo: pathlib.Path, value: str) -> pathlib.Path:
    if value == LATEST_RESUME:
        return latest_state_path(repo)
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
    data.setdefault("verbose", False)
    data.setdefault("final_review_path", "")
    data.setdefault("blocked_reason", "")
    data.setdefault("blocked_resume_phase", "")
    data.setdefault("pending_review_path", "")
    data.setdefault("pending_review_round", 0)
    data.setdefault("source_plan_sha256", "")
    repo = pathlib.Path(data.get("repo") or path.parents[3]).expanduser()
    data.setdefault("decision_log_path", str(decision_log_path(repo, data["run_id"]).relative_to(repo)))
    return RunState(**data)


def migrate_legacy_pending_fix(repo: pathlib.Path, state: RunState) -> str | None:
    """Recover reviews persisted before pending-fix checkpoints were added."""
    if (
        state.gh_review
        or state.phase != "implementation_ready"
        or state.pending_review_path
        or state.local_review_round < 1
    ):
        return None
    round_index = state.local_review_round
    review_path = repo / ".a2a" / "reviews" / state.run_id / f"review-{round_index}.md"
    review = read_if_present(review_path)
    if not ends_with_token(review, REVIEW_CHANGES_TOKEN):
        return None
    decisions = read_if_present(resolve_repo_path(repo, pathlib.Path(state.decision_log_path)))
    review_heading = f"## Local review round {round_index}: changes requested"
    fix_heading = f"## Local fix round {round_index}: implemented"
    if review_heading not in decisions or fix_heading in decisions:
        return None
    review_rel = repo_relative(repo, review_path)
    state.phase = "local_fix_pending"
    state.pending_review_path = review_rel
    state.pending_review_round = round_index
    return review_rel


def summarize_agent_output(output: str, max_len: int = 220) -> str:
    skip_tokens = {
        APPROVAL_TOKEN,
        PLAN_READY_TOKEN,
        PLAN_REVIEW_READY_TOKEN,
        PLAN_APPROVAL_TOKEN,
        PLAN_BLOCKED_TOKEN,
        PLAN_CHANGES_TOKEN,
        REVIEW_CHANGES_TOKEN,
        IMPLEMENTATION_READY_TOKEN,
        IMPLEMENTATION_BLOCKED_TOKEN,
    }
    for raw_line in output.splitlines():
        line = compact_line(raw_line, max_len)
        if not line or line in skip_tokens:
            continue
        if line.startswith(DECISION_REASON_PREFIX):
            continue
        if line in {
            PLAN_UPDATE_BEGIN,
            PLAN_UPDATE_END,
            PLAN_APPEND_BEGIN,
            PLAN_APPEND_END,
            COMMIT_MESSAGE_BEGIN,
            COMMIT_MESSAGE_END,
        }:
            continue
        if is_code_like_verbose_line(line):
            continue
        return line
    return "No short reason captured; see run log for full output."


def extract_decision_reason(output: str, max_len: int = 220) -> str | None:
    for raw_line in reversed(output.splitlines()):
        line = raw_line.strip()
        if not line.startswith(DECISION_REASON_PREFIX):
            continue
        reason = compact_line(line[len(DECISION_REASON_PREFIX) :], max_len)
        return reason or None
    return None


def decision_reason(output: str) -> str:
    return extract_decision_reason(output) or summarize_agent_output(output)


def implementation_status(output: str) -> str:
    final_line = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "")
    if final_line == IMPLEMENTATION_READY_TOKEN:
        return "ready"
    if final_line == IMPLEMENTATION_BLOCKED_TOKEN:
        return "blocked"
    return "missing"


def append_decision(
    repo: pathlib.Path,
    state: RunState,
    dry_run: bool,
    trace: WorkflowTrace,
    title: str,
    fields: list[tuple[str, str | None]],
) -> None:
    path = resolve_repo_path(repo, pathlib.Path(state.decision_log_path))
    lines = [f"## {title}", ""]
    for key, value in fields:
        if value:
            lines.append(f"- {key}: {value}")
    lines.append("")
    if dry_run:
        trace.event(f"dry-run would append decision log: {repo_relative(repo, path)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        header = textwrap.dedent(
            f"""\
            # A2A Decision Log

            - Run: {state.run_id}
            - Goal: {state.goal}
            - Branch: {state.branch}
            - Base: {state.base}

            """
        )
        path.write_text(header, encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")
    trace.event(f"decision log appended: {repo_relative(repo, path)}")


def mark_run_blocked(
    repo: pathlib.Path,
    state: RunState,
    dry_run: bool,
    trace: WorkflowTrace,
    context: str,
    reason: str,
    resume_phase: str,
) -> None:
    state.phase = "blocked"
    state.blocked_reason = reason
    state.blocked_resume_phase = resume_phase
    append_decision(
        repo,
        state,
        dry_run,
        trace,
        f"{context}: blocked",
        [
            ("Reason", reason),
            ("Resume phase", resume_phase),
        ],
    )
    save_state(repo, state, dry_run, trace)
    trace.event(f"blocked: {reason}")
    trace.event(
        "resolve the blocker, then retry with: "
        f"a2a-loop --resume {state.run_id} --retry-blocked"
    )


def blocked_exit(state: RunState, log: pathlib.Path, trace: WorkflowTrace) -> int:
    if state.blocked_reason:
        trace.event(f"run remains blocked: {state.blocked_reason}")
    trace.event(
        "retry only after resolving it: "
        f"a2a-loop --resume {state.run_id} --retry-blocked"
    )
    trace.event(f"logs: {log}")
    return 3


def run(
    args: list[str],
    cwd: pathlib.Path,
    dry_run: bool,
    log_file: pathlib.Path,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    stream_output: bool = False,
    stream_label: str | None = None,
    stdout_line_handler: Callable[[str], None] | None = None,
    stderr_line_handler: Callable[[str], None] | None = None,
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
    stdout_prefix = f"[{stream_label}:stdout] " if stream_label else ""
    stderr_prefix = f"[{stream_label}:stderr] " if stream_label else ""

    def pump_stdout() -> None:
        # Stream stdout into the run log as it arrives so long agent turns
        # stay observable via tail -f instead of appearing all at once.
        assert proc.stdout is not None
        with log_file.open("a", encoding="utf-8") as f:
            for line in proc.stdout:
                stdout_lines.append(line)
                f.write(line)
                f.flush()
                if stream_output:
                    if stdout_line_handler:
                        stdout_line_handler(line.rstrip("\n"))
                    else:
                        print(f"{stdout_prefix}{line}", end="")

    def pump_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            if stream_output:
                if stderr_line_handler:
                    stderr_line_handler(line.rstrip("\n"))
                else:
                    print(f"{stderr_prefix}{line}", end="", file=sys.stderr)

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
    if proc.stdout:
        proc.stdout.close()
    if proc.stderr:
        proc.stderr.close()

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


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", value)


def compact_line(value: str, limit: int = VERBOSE_LINE_LIMIT) -> str:
    line = re.sub(r"\s+", " ", strip_ansi(value)).strip()
    if len(line) <= limit:
        return line
    return line[: limit - 3].rstrip() + "..."


def verbose_emit(agent: str, message: str) -> None:
    line = compact_line(message)
    if line:
        print(f"[{agent}] {line}")


def is_code_like_verbose_line(line: str) -> bool:
    stripped = strip_ansi(line).strip()
    if not stripped:
        return True
    code_prefixes = (
        "```",
        "@@",
        "diff --git",
        "index ",
        "--- ",
        "+++ ",
        "+",
        "-",
        "import ",
        "from ",
        "export ",
        "const ",
        "let ",
        "var ",
        "function ",
        "class ",
        "def ",
        "return ",
    )
    if stripped.startswith(code_prefixes):
        return True
    if re.match(r"^\d+\s*[|:]\s*", stripped):
        return True
    if re.match(r"^[}\])];,]+$", stripped):
        return True
    if re.search(r"[{}();=<>]", stripped) and len(stripped.split()) <= 12:
        return True
    return False


def make_text_verbose_handler(agent: str) -> Callable[[str], None]:
    noise = (
        "WARNING: proceeding, even though",
        "Run Codex non-interactively",
        "succeeded in ",
    )
    in_code_block = False

    def handle(line: str) -> None:
        nonlocal in_code_block
        raw = strip_ansi(line).strip()
        if raw.startswith("```"):
            in_code_block = not in_code_block
            return
        if in_code_block or is_code_like_verbose_line(raw):
            return
        clean = compact_line(line)
        if not clean or any(clean.startswith(prefix) for prefix in noise):
            return
        verbose_emit(agent, clean)

    return handle


def summarize_tool_input(tool_name: str, tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        return ""
    path = tool_input.get("file_path") or tool_input.get("path")
    if isinstance(path, str) and path.strip():
        return f": {path}"
    for key in ("command", "cmd", "pattern", "query", "file_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f": {compact_line(value, 180)}"
    return ""


def summarize_tool_result(content: object) -> str | None:
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        content = "\n".join(parts)
    if not isinstance(content, str):
        return None
    lines = [compact_line(line, 180) for line in content.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None
    return lines[0]


def make_claude_verbose_handler(agent: str) -> Callable[[str], None]:
    in_code_block = False

    def handle(line: str) -> None:
        nonlocal in_code_block
        if not line.strip():
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            verbose_emit(agent, line)
            return
        event_type = event.get("type")
        if event_type == "system":
            subtype = event.get("subtype")
            tools = event.get("tools")
            if subtype == "init":
                if isinstance(tools, list) and tools:
                    verbose_emit(agent, "session started; tools: " + ", ".join(str(tool) for tool in tools[:8]))
                else:
                    verbose_emit(agent, "session started")
            return
        if event_type == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                return
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "text" and isinstance(item.get("text"), str):
                    for text_line in item["text"].splitlines():
                        raw = strip_ansi(text_line).strip()
                        if raw.startswith("```"):
                            in_code_block = not in_code_block
                            continue
                        if in_code_block or is_code_like_verbose_line(raw):
                            continue
                        verbose_emit(agent, text_line)
                elif item_type == "tool_use":
                    name = str(item.get("name") or "tool")
                    verbose_emit(agent, f"tool: {name}{summarize_tool_input(name, item.get('input'))}")
            return
        if event_type == "user":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                return
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    continue
                verbose_emit(agent, "tool result received")
            return
        if event_type == "result":
            subtype = event.get("subtype") or event.get("status") or "done"
            verbose_emit(agent, f"turn {subtype}")

    return handle


def parse_claude_stream_json_output(stdout: str) -> str:
    result_text = ""
    assistant_text: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            result_text = event["result"]
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                assistant_text.append(item["text"])
    if result_text:
        return result_text
    return "\n".join(assistant_text)


def emit_verbose_worktree_summary(agent: str, repo: pathlib.Path, dry_run: bool, log: pathlib.Path) -> None:
    if dry_run:
        return
    diff = run(["git", "diff", "--stat"], cwd=repo, dry_run=dry_run, log_file=log)
    if diff.returncode != 0:
        return
    status = run(["git", "status", "--short"], cwd=repo, dry_run=dry_run, log_file=log)
    if status.returncode != 0:
        return
    diff_lines = [compact_line(line) for line in diff.stdout.splitlines() if line.strip()]
    untracked = [
        compact_line(line[3:])
        for line in status.stdout.splitlines()
        if line.startswith("?? ") and line[3:].strip()
    ]
    if not diff_lines and not untracked:
        verbose_emit(agent, "worktree changes: none")
        return
    verbose_emit(agent, "worktree changes:")
    for line in diff_lines[:20]:
        verbose_emit(agent, f"  {line}")
    for path in untracked[:20]:
        verbose_emit(agent, f"  untracked: {path}")
    omitted = max(0, len(diff_lines) + len(untracked) - 40)
    if omitted:
        verbose_emit(agent, f"  ... {omitted} more entries")


def claude_env(use_api_key: bool) -> dict[str, str] | None:
    if use_api_key:
        return None
    env = os.environ.copy()
    for name in CLAUDE_API_AUTH_ENV_VARS:
        env.pop(name, None)
    return env


def claude_allowed_tools(phase: str) -> tuple[str, ...]:
    if phase in CLAUDE_REVIEW_PHASES:
        return CLAUDE_REVIEW_ALLOWED_TOOLS
    return ()


def claude_print(
    prompt: str,
    repo: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
    model: str | None,
    effort: str | None,
    use_api_key: bool,
    verbose: bool = False,
    phase: str = "",
) -> str:
    args = [
        "claude",
        "-p",
        "--permission-mode",
        "dontAsk",
        "--output-format",
        "stream-json" if verbose else "text",
    ]
    allowed_tools = claude_allowed_tools(phase)
    if allowed_tools:
        args.extend(["--allowedTools", *allowed_tools])
    if verbose:
        args.append("--verbose")
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
        stream_output=verbose,
        stream_label="claude",
        stdout_line_handler=make_claude_verbose_handler("claude") if verbose else None,
    )
    require_ok(result, "Claude turn")
    output = parse_claude_stream_json_output(result.stdout) if verbose else result.stdout
    require_no_agent_error(
        CmdResult(args=result.args, returncode=result.returncode, stdout=output, stderr=result.stderr),
        "Claude turn",
    )
    return output


def codex_exec(
    prompt: str,
    repo: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
    model: str | None,
    effort: str | None,
    verbose: bool = False,
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
        stream_output=verbose,
        stream_label="codex",
        stdout_line_handler=make_text_verbose_handler("codex") if verbose else None,
        stderr_line_handler=make_text_verbose_handler("codex") if verbose else None,
    )
    require_ok(result, "Codex turn")
    require_no_agent_error(result, "Codex turn")
    return result.stdout


def raw_step_log_path(log: pathlib.Path, step: int, agent: str, phase: str) -> pathlib.Path:
    phase_slug = slugify_goal(phase)[:40]
    return log.parent / "steps" / f"step-{step:02d}-{agent}-{phase_slug}.log"


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
        agent_log = raw_step_log_path(log, step, agent, phase)
        output = claude_print(
            prompt,
            repo,
            dry_run,
            agent_log,
            state.claude_model,
            state.claude_effort,
            state.claude_use_api_key,
            state.verbose,
            phase,
        )
        if state.verbose:
            emit_verbose_worktree_summary(agent, repo, dry_run, log)
        trace.finish_agent(agent, step, phase, model, effort, output)
        trace.event(f"step {step} raw transcript: {display_path(repo, agent_log)}")
        return output
    if agent == "codex":
        model = state.codex_display_model
        effort = state.codex_display_effort
        step = trace.start_agent(agent, phase, model, effort, artifact)
        agent_log = raw_step_log_path(log, step, agent, phase)
        output = codex_exec(prompt, repo, dry_run, agent_log, state.codex_model, state.codex_effort, state.verbose)
        if state.verbose:
            emit_verbose_worktree_summary(agent, repo, dry_run, log)
        trace.finish_agent(agent, step, phase, model, effort, output)
        trace.event(f"step {step} raw transcript: {display_path(repo, agent_log)}")
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


def checkout_branch(repo: pathlib.Path, branch: str, dry_run: bool, log: pathlib.Path) -> None:
    result = run(["git", "checkout", branch], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(result, "branch checkout")


def branch_exists(repo: pathlib.Path, branch: str, dry_run: bool, log: pathlib.Path) -> bool:
    result = run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo, dry_run=dry_run, log_file=log)
    if dry_run:
        return False
    return result.returncode == 0


def ensure_branch(
    repo: pathlib.Path,
    branch: str,
    base: str,
    dry_run: bool,
    log: pathlib.Path,
    trace: WorkflowTrace,
) -> None:
    if branch_exists(repo, branch, dry_run, log):
        checkout_branch(repo, branch, dry_run, log)
        return
    # New agent branches always start from the freshest base available so
    # stacked PRs and stale local state never leak into the reviewed diff
    # (squash-merge repos turn stacked branches into conflict surgery).
    fetch = run(["git", "fetch", "origin", base], cwd=repo, dry_run=dry_run, log_file=log)
    candidates = [f"origin/{base}"] if fetch.returncode == 0 else []
    candidates.append(base)
    for start in candidates:
        result = run(["git", "checkout", "-b", branch, start], cwd=repo, dry_run=dry_run, log_file=log)
        if result.returncode == 0:
            trace.event(f"branch {branch} created from {start}")
            return
    trace.event(f"base ref {base} unavailable; branching {branch} from current HEAD")
    result = run(["git", "checkout", "-b", branch], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(result, "branch setup")


def codex_code_mode_host_status() -> str | None:
    codex_command = shutil.which("codex")
    if not codex_command:
        return None
    launcher = pathlib.Path(codex_command)
    bundled_host = launcher.resolve().parent / "codex-code-mode-host"
    if not bundled_host.exists():
        raise SystemExit(
            "Codex code-mode host is missing from the installed CLI: "
            f"{bundled_host}\nRepair or update the Codex installation before starting an agent turn."
        )
    expected_host = launcher.parent / "codex-code-mode-host"
    if not expected_host.exists():
        raise SystemExit(
            "Codex code-mode host alias is missing: "
            f"{expected_host}\nThe bundled host exists at: {bundled_host}\n"
            "Repair the Codex installation or create the sibling alias before starting an agent turn."
        )
    return str(expected_host)


def trace_capability_manifest(
    repo: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
    trace: WorkflowTrace,
) -> None:
    # Probed up front so a run never discovers mid-turn that a tool, scope,
    # or remote it depends on is missing.
    clis = ", ".join(
        f"{name}={'ok' if shutil.which(name) else 'MISSING'}"
        for name in ("git", "gh", "claude", "codex")
    )
    trace.event(f"capabilities: CLIs: {clis}")
    trace.event(f"capabilities: repo writable: {'yes' if os.access(repo, os.W_OK) else 'NO'}")
    host = codex_code_mode_host_status()
    if host:
        trace.event(f"capabilities: Codex code-mode host: {host}")
    if dry_run:
        trace.event("capabilities: origin remote: not probed in dry-run")
        return
    try:
        remote = run(
            ["git", "ls-remote", "--exit-code", "origin", "HEAD"],
            cwd=repo,
            dry_run=False,
            log_file=log,
            timeout=30,
        )
        reachable = "reachable" if remote.returncode == 0 else "UNREACHABLE"
    except SystemExit:
        reachable = "UNREACHABLE (probe timed out)"
    trace.event(f"capabilities: origin remote: {reachable}")


def slugify_goal(goal: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")
    return slug[:64].strip("-") or "a2a-plan"


def slugify_plan_name(path: pathlib.Path) -> str:
    name = path.name
    if name.endswith(".plan.md"):
        name = name[: -len(".plan.md")]
    else:
        name = path.stem
    return slugify_goal(name)


def default_branch_name(plan_slug: str, stamp: str) -> str:
    date = stamp.split("-", 1)[0]
    return f"a2a/{plan_slug}-{date}"


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
        f"decisions={display_path(repo, resolve_repo_path(repo, pathlib.Path(state.decision_log_path)))}, "
        f"log={display_path(repo, pathlib.Path(state.log_path))}, "
        f"raw-steps={display_path(repo, pathlib.Path(state.log_path).parent / 'steps')}"
    )
    if state.source_plan_path and state.source_plan_path != state.plan_path:
        trace.event(f"plan ledger source: {state.source_plan_path}")


def plan_is_in_a2a(repo: pathlib.Path, path: pathlib.Path) -> bool:
    return display_path(repo, path).startswith(".a2a/")


def plan_digest(contents: str) -> str:
    return hashlib.sha256(contents.encode("utf-8")).hexdigest()


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


def sync_source_plan(
    repo: pathlib.Path,
    working_plan_path: pathlib.Path,
    state: RunState,
    dry_run: bool,
    trace: WorkflowTrace,
) -> bool:
    if not state.source_plan_path or state.source_plan_path == state.plan_path:
        return True
    source_path = resolve_repo_path(repo, pathlib.Path(state.source_plan_path))
    if dry_run:
        trace.event(
            f"dry-run would sync plan ledger back to source: "
            f"{display_path(repo, working_plan_path)} -> {display_path(repo, source_path)}"
        )
        return True
    if not working_plan_path.exists() or not source_path.exists():
        missing = working_plan_path if not working_plan_path.exists() else source_path
        mark_run_blocked(
            repo,
            state,
            dry_run,
            trace,
            "Plan synchronization",
            f"plan synchronization cannot continue because {display_path(repo, missing)} is missing",
            "plan_written",
        )
        return False
    working = working_plan_path.read_text(encoding="utf-8")
    existing = source_path.read_text(encoding="utf-8")
    working_sha256 = plan_digest(working)
    source_sha256 = plan_digest(existing)
    if working == existing:
        state.source_plan_sha256 = source_sha256
        return True
    last_synced = state.source_plan_sha256
    if not last_synced:
        mark_run_blocked(
            repo,
            state,
            dry_run,
            trace,
            "Plan synchronization",
            "legacy checkpoint has divergent source and working plans; reconcile them manually before retrying",
            "plan_written",
        )
        return False
    if source_sha256 == last_synced:
        source_path.write_text(working, encoding="utf-8")
        state.source_plan_sha256 = working_sha256
        trace.event(f"synced plan ledger back to source: {display_path(repo, source_path)}")
        return True
    if working_sha256 == last_synced:
        working_plan_path.write_text(existing, encoding="utf-8")
        state.source_plan_sha256 = source_sha256
        trace.event(
            f"imported operator source-plan changes into run ledger: "
            f"{display_path(repo, working_plan_path)}"
        )
        return True
    mark_run_blocked(
        repo,
        state,
        dry_run,
        trace,
        "Plan synchronization",
        "canonical source plan and run ledger changed independently; reconcile both copies manually",
        "plan_written",
    )
    return False


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


def plan_headings(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if re.match(r"^#{1,6}\s", line)]


def missing_headings(before: list[str], after: list[str]) -> list[str]:
    missing: list[str] = []
    for heading in dict.fromkeys(before):
        if before.count(heading) > after.count(heading):
            missing.append(heading)
    return missing


def extract_constraint_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if any(marker in line for marker in CONSTRAINT_MARKERS)
    ]


def extract_closeout(plan_text: str) -> str | None:
    """Return the plan's Closeout section when it carries all required labels."""
    lines = plan_text.splitlines()
    start = None
    level = 0
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+Closeout\b", line, re.IGNORECASE)
        if match:
            start = index
            level = len(match.group(1))
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start + 1 :]:
        match = re.match(r"^(#{1,6})\s", line)
        if match and len(match.group(1)) <= level:
            break
        body.append(line)
    section = "\n".join(body).strip()
    if not section:
        return None
    if not all(label in section for label in CLOSEOUT_REQUIRED_LABELS):
        return None
    return section


def conventions_note(repo: pathlib.Path) -> str:
    found = [name for name in CONVENTION_FILES if (repo / name).exists()]
    if not found:
        return ""
    return (
        "Repo operating manual: read " + ", ".join(found) + " before your first "
        "edit and honor its named conventions; it outranks your defaults.\n\n"
    )


def constraints_block(plan_text: str) -> str:
    lines = extract_constraint_lines(plan_text)
    if not lines:
        return ""
    return (
        "Hard constraints found in the plan (echo and honor them; they are "
        "protected by the append-only rule):\n"
        + "\n".join(f"- {line}" for line in lines)
        + "\n\n"
    )


def require_headings_preserved(existing: str, updated: str, context: str) -> None:
    """Reject a plan update that deletes body sections.

    The plan is the run's contract: agents may update todo statuses and
    append sections, but a heading that exists on disk must survive every
    update. The coordinator owns the write path, so a violating update is
    rejected before anything touches the file.
    """
    missing = missing_headings(plan_headings(existing), plan_headings(updated))
    if not missing:
        return
    raise SystemExit(
        f"Plan update rejected ({context}): it would delete sections: "
        + "; ".join(missing)
        + "\nThe plan body is append-only: updates may change todo statuses "
        "and append notes, but must never delete or rewrite existing "
        "sections.\nThe plan file on disk was left untouched. Resume the run "
        "to retry the turn, or append the update manually."
    )


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


def strip_artifact_tokens(output: str, tokens: set[str]) -> str:
    lines = output.strip().splitlines()
    while lines:
        tail = lines[-1].strip()
        if tail in tokens or tail.startswith(DECISION_REASON_PREFIX):
            lines.pop()
            continue
        break
    return "\n".join(lines).strip()


def persist_plan_markdown(
    path: pathlib.Path,
    output: str,
    dry_run: bool,
    trace: WorkflowTrace,
    reason: str,
    tokens: set[str],
) -> bool:
    plan = strip_artifact_tokens(output, tokens)
    if not plan:
        trace.event(f"plan output empty; no coordinator write for {path.name}")
        return False
    if dry_run:
        trace.event(f"dry-run would persist plan stdout ({reason}): {path.name}")
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing.strip() == plan.strip():
        trace.event(f"plan unchanged after {reason}: {path.name}")
        return False
    require_headings_preserved(existing, plan, reason)
    path.write_text(plan.rstrip() + "\n", encoding="utf-8")
    trace.event(f"persisted plan stdout ({reason}): {path.name}")
    return True


def extract_sentinel_block(output: str, begin: str, end: str) -> str | None:
    pattern = re.compile(
        rf"{re.escape(begin)}\s*\n(?P<body>.*?)\n\s*{re.escape(end)}",
        re.DOTALL,
    )
    match = pattern.search(output)
    if not match:
        return None
    body = match.group("body").strip()
    return body or None


def extract_plan_update(output: str) -> str | None:
    return extract_sentinel_block(output, PLAN_UPDATE_BEGIN, PLAN_UPDATE_END)


def extract_plan_append(output: str) -> str | None:
    return extract_sentinel_block(output, PLAN_APPEND_BEGIN, PLAN_APPEND_END)


def persist_plan_append_block(
    path: pathlib.Path,
    output: str,
    dry_run: bool,
    trace: WorkflowTrace,
    reason: str,
) -> bool:
    addition = extract_plan_append(output)
    if addition is None:
        return False
    first_line = next((line.strip() for line in addition.splitlines() if line.strip()), "")
    if not first_line.startswith("#"):
        raise SystemExit(f"Plan append rejected ({reason}): appended markdown must begin with a heading.")
    if dry_run:
        trace.event(f"dry-run would append plan delta ({reason}): {path.name}")
        return True
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if addition.strip() in existing:
        trace.event(f"plan delta already present after {reason}: {path.name}")
        return False
    separator = "\n\n" if existing.strip() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing.rstrip() + separator + addition.rstrip() + "\n", encoding="utf-8")
    trace.event(f"appended plan delta ({reason}): {path.name}")
    return True


def persist_plan_revision(
    path: pathlib.Path,
    output: str,
    dry_run: bool,
    trace: WorkflowTrace,
    reason: str,
    tokens: set[str],
) -> bool:
    if extract_plan_append(output) is not None:
        return persist_plan_append_block(path, output, dry_run, trace, reason)
    return persist_plan_markdown(path, output, dry_run, trace, reason, tokens)


def persist_plan_update_block(
    path: pathlib.Path,
    output: str,
    dry_run: bool,
    trace: WorkflowTrace,
    reason: str,
) -> bool:
    plan = extract_plan_update(output)
    if plan is None:
        return False
    return persist_plan_markdown(path, plan, dry_run, trace, reason, tokens=set())


def extract_commit_message(output: str) -> str | None:
    message = extract_sentinel_block(output, COMMIT_MESSAGE_BEGIN, COMMIT_MESSAGE_END)
    if message is None:
        return None
    lines = [re.sub(r"\s+", " ", line).strip() for line in message.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None
    return lines[0][:120]


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
    state: RunState,
    plan_path: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
) -> str:
    base, branch, goal = state.base, state.branch, state.goal
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

    plan_text = read_if_present(plan_path)
    closeout = extract_closeout(plan_text)
    if closeout is None:
        raise SystemExit(
            "No PR opened: the plan has no valid `## Closeout` section.\n"
            "Required labels, each on its own line: Verified:, "
            "Attempted-blocked (cause):, Deferred (tracked in):, Not claimed:.\n"
            f"Append the closeout to {plan_path} (or resume so the "
            f"implementer adds it): a2a-loop --resume {state.run_id}"
        )
    constraints = extract_constraint_lines(plan_text)
    briefing = ""
    if state.final_review_path:
        briefing = read_if_present(
            resolve_repo_path(repo, pathlib.Path(state.final_review_path))
        ).strip()

    ensure_branch_has_pr_commits(repo, base, branch, dry_run, log)
    push_result = run(["git", "push", "-u", "origin", branch], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(push_result, "branch push")

    sections = [f"## Goal\n\n{goal}"]
    if constraints:
        sections.append(
            "## Constraints acknowledged\n\n"
            + "\n".join(f"- {line}" for line in constraints)
        )
    sections.append(f"## Agent Plan\n\n{plan_text.strip()}")
    if briefing:
        sections.append(f"## Reviewer briefing\n\n{briefing}")
    sections.append(f"## Closeout\n\n{closeout}")
    sections.append(
        "## Coordination\n\n"
        "This PR was opened by the a2a-loop coordinator. Merge decisions "
        "belong to a human or a separate review session."
    )
    body = "\n\n".join(sections)
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


def ensure_branch_has_pr_commits(
    repo: pathlib.Path,
    base: str,
    branch: str,
    dry_run: bool,
    log: pathlib.Path,
) -> None:
    if dry_run:
        return
    ahead = run(["git", "rev-list", "--count", f"{base}..{branch}"], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(ahead, "PR commit preflight")
    try:
        count = int(ahead.stdout.strip())
    except ValueError as exc:
        raise SystemExit(f"Unexpected git rev-list output for {base}..{branch}: {ahead.stdout!r}") from exc
    if count > 0:
        return

    status = run(["git", "status", "--porcelain"], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(status, "working tree status after empty PR preflight")
    dirty_hint = ""
    if status.stdout.strip():
        dirty_hint = "\n\nWorking tree still has uncommitted changes:\n" + status.stdout.strip()
    else:
        dirty_hint = (
            "\n\nWorking tree is clean, so the approved run did not create any committable "
            "source changes. It may have only updated ignored .a2a artifacts, or the agent "
            "completed with no product diff."
        )
    raise SystemExit(
        f"No PR opened: branch {branch} has no commits ahead of {base}."
        f"{dirty_hint}\n\n"
        "Inspect with:\n"
        f"  git log --oneline {base}..{branch}\n"
        f"  git diff --stat {base}...{branch}\n"
        f"  git status --short\n"
    )


def commit_if_changed(
    repo: pathlib.Path,
    message: str,
    dry_run: bool,
    log: pathlib.Path,
    trace: WorkflowTrace,
) -> str | None:
    if dry_run:
        trace.event(f"dry-run would create coordinator commit: {message}")
        return "DRY_RUN_COMMIT"
    status = run(["git", "status", "--porcelain"], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(status, "working tree status")
    if not status.stdout.strip():
        trace.event(f"no changes to commit after: {message}")
        return None
    add_result = run(["git", "add", "-A"], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(add_result, "stage coordinator commit")
    diff_result = run(["git", "diff", "--cached", "--quiet"], cwd=repo, dry_run=dry_run, log_file=log)
    if diff_result.returncode == 0:
        trace.event(f"no staged changes to commit after: {message}")
        return None
    if diff_result.returncode != 1:
        require_ok(diff_result, "staged diff check")
    commit_result = run(["git", "commit", "-m", message], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(commit_result, "coordinator commit")
    rev_result = run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, dry_run=dry_run, log_file=log)
    require_ok(rev_result, "coordinator commit rev")
    commit_hash = rev_result.stdout.strip()
    trace.event(f"coordinator commit created: {commit_hash} {message}")
    return commit_hash


def short_commit_goal(goal: str) -> str:
    return re.sub(r"\s+", " ", goal).strip()[:64] or "a2a changes"


def commit_message_from_output(output: str, fallback: str) -> str:
    return extract_commit_message(output) or fallback


def build_plan_prompt(goal: str, base: str, plan_path: str, conventions: str = "") -> str:
    return f"""
You are the planner in an agent-to-agent workflow.

Target goal:
{goal}

Base branch: {base}

{conventions}Return the complete implementation plan in stdout. The coordinator will persist it to:
{plan_path}
Do not include commentary outside the plan markdown except the final {PLAN_READY_TOKEN} line.

Use the dirtybits/agent-skills `plan-writing` convention for `.plan.md` files:
- Start with YAML frontmatter containing `name`, `overview`, `todos`, and `isProject`.
- Make `todos` a short checklist with stable lowercase hyphenated `id` values,
  concrete `content`, and `status: pending`.
- After frontmatter, include Markdown sections for goal, scope, files to change,
  implementation steps, verification, rollout/rollback if relevant, and blockers.
- Give each todo explicit done-when criteria so completion can be verified,
  not asserted.
- Mark hard ordering or decision constraints with explicit `SEQUENCING:` or
  `DECISION:` callouts; they are a control channel to the implementer and
  reviewer and must survive every later plan update.
- Include enough repo-specific detail that the implementer can proceed without guessing.

Do not write plan files yourself. Do not edit source files. End your response with {PLAN_READY_TOKEN}.
""".strip()


def build_plan_review_prompt(goal: str, base: str, plan_path: str, conventions: str = "") -> str:
    return f"""
You are the implementer, reviewing the plan before implementation.

Goal:
{goal}

Base branch: {base}

{conventions}Read the current plan at:
{plan_path}

Inspect the repo enough to catch missing steps, risky assumptions, weak tests,
or repo-specific implementation details. Return only a new "Implementer
Review" or "Implementation Enhancements" section between:
{PLAN_APPEND_BEGIN}
...append-only markdown beginning with a heading...
{PLAN_APPEND_END}
Do not repeat the existing plan or its frontmatter.

The plan body is append-only: add sections and notes, but never delete or
rewrite existing sections or drop SEQUENCING/DECISION callouts — the
coordinator rejects updates that delete sections.
Immediately before the final token, include one concise line beginning
`{DECISION_REASON_PREFIX}` that summarizes what the review added.

Do not write plan files yourself. Do not implement the feature yet. End your response with {PLAN_REVIEW_READY_TOKEN}.
""".strip()


def build_plan_approval_prompt(goal: str, base: str, plan_path: str) -> str:
    return f"""
You are the reviewer/plan gate, approving the implementation plan before code edits begin.

Goal:
{goal}

Base branch: {base}

Read and review the enhanced plan at:
{plan_path}

If the plan is ready for implementation, end with exactly:
{PLAN_APPROVAL_TOKEN}

If changes are still needed, return only a concise "Reviewer Follow-up"
section between:
{PLAN_APPEND_BEGIN}
...append-only markdown beginning with a heading...
{PLAN_APPEND_END}
and end with exactly:
{PLAN_CHANGES_TOKEN}
The plan body is append-only: add your follow-up section, but never delete or
rewrite existing sections or drop SEQUENCING/DECISION callouts.

If implementation is blocked on a human-only choice, external authority, or
other condition that another autonomous plan-review round cannot resolve, do
not append another follow-up. Explain the blocker, then end with exactly:
{PLAN_BLOCKED_TOKEN}

Immediately before either final status token, include one concise line
beginning `{DECISION_REASON_PREFIX}` explaining the decision.

Do not write plan files yourself. Do not implement the feature.
""".strip()


def build_implement_prompt(
    goal: str,
    plan_path: str,
    base: str,
    conventions: str = "",
    constraints: str = "",
) -> str:
    return f"""
You are the implementer in an agent-to-agent workflow.

Goal:
{goal}

Plan file:
{plan_path}

{conventions}{constraints}Instructions:
- Read the plan file before editing.
- Inspect the repo before editing.
- First, pre-classify each plan verification step as runnable or blocked in
  this environment (network access, secrets, build tools, browsers). State
  the classification up front and record blocked steps as deferred in the
  plan update now, instead of discovering them mid-run.
- Track plan todo status mentally while working. If todo status/body should
  change, include a complete updated plan in stdout between:
  {PLAN_UPDATE_BEGIN}
  ...complete updated plan markdown...
  {PLAN_UPDATE_END}
  The coordinator will persist the plan update.
- The plan body is append-only: update todo statuses and append dated
  progress/divergence notes at the point of divergence, but never delete or
  rewrite existing sections (Goal, Scope, Rollback, decision or sequencing
  notes). The coordinator rejects updates that delete sections.
- Treat SEQUENCING, DECISION, stop-the-line, and founder-acked notes in the
  plan as hard constraints.
- Set a todo to `completed` only when every done-when item is met; otherwise
  leave it `in_progress` and append a dated deferral note naming where the
  remainder is tracked.
- Implement the smallest complete change that satisfies the plan.
- Run relevant tests/checks. Report each result together with the working
  directory it ran in and the exact command, so results are bound to this
  worktree and not another checkout.
- Maintain a `## Closeout` section in the plan update with exactly these
  four labels, each starting its own line: `Verified:`,
  `Attempted-blocked (cause):`, `Deferred (tracked in):`, `Not claimed:`.
  The coordinator refuses to open a PR without it.
- Optionally suggest the coordinator commit subject in stdout between:
  {COMMIT_MESSAGE_BEGIN}
  concise commit subject
  {COMMIT_MESSAGE_END}
- Do not commit. The coordinator will create the git commit after your turn.
- Do not write to `.git`.
- Do not write `.a2a` plan/review files yourself.
- Do not push.
- Do not merge.
- Immediately before the final status token, include one concise line
  beginning `{DECISION_REASON_PREFIX}` summarizing completion or the blocker.
- End with exactly `{IMPLEMENTATION_READY_TOKEN}` only when implementation is
  ready to commit and review.
- If any hard constraint or required operator decision blocks completion, do
  not claim readiness; end with exactly `{IMPLEMENTATION_BLOCKED_TOKEN}`.

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

Return your complete review in stdout. The coordinator will persist it to:
{review_path}

Review duties beyond the diff itself:
- Verify plan todo statuses match reality: a todo may be `completed` only if
  every done-when item is met. Overclaimed statuses are changes to request.
- Verify the plan's `## Closeout` section is honest: nothing under
  `Verified:` that was not actually run, and blocked/deferred work named as
  such.
- Verify claimed test results are plausible for this worktree and diff;
  re-run cheap checks yourself when in doubt.
- Run allowed verification commands directly, without shell setup prefixes,
  command chaining, pipes, or output redirection; the coordinator grants a
  narrow non-interactive allowlist for read-only Git, standard test runners,
  and PR inspection.
- If other open PRs touch the same subsystem, state the required merge order
  and semantic-conflict risks. Never call this change independent of them
  unless the combination was actually tested.

If changes are needed:
- Include actionable findings in your stdout review.
- Immediately before the final token, include one concise line beginning
  `{DECISION_REASON_PREFIX}` explaining the most important reason.
- End your response with exactly:
{REVIEW_CHANGES_TOKEN}

If the implementation satisfies the goal and tests are adequate:
- Include a concise approval summary in your stdout review.
- Include a `## Reviewer briefing` section for the human or external
  reviewer: the riskiest hunks, the invariants to verify, what to try to
  break, and what earlier internal review rounds already caught and fixed
  (so it is not re-litigated). This section is copied into the PR body.
- Immediately before the final token, include one concise line beginning
  `{DECISION_REASON_PREFIX}` explaining why the change is ready.
- End your response with exactly:
{APPROVAL_TOKEN}

Do not write review files yourself. Do not merge, push, or edit source files.
""".strip()


def build_local_fix_prompt(
    goal: str,
    base: str,
    plan_path: str,
    review_path: str,
    constraints: str = "",
) -> str:
    return f"""
You are the fixer in a local-first agent-to-agent workflow.

Goal:
{goal}

Plan file:
{plan_path}

Review file:
{review_path}

{constraints}Instructions:
- Read the plan and review files.
- Inspect the local diff against `{base}`.
- Track plan todo status mentally while working. If todo status/body should
  change, include a complete updated plan in stdout between:
  {PLAN_UPDATE_BEGIN}
  ...complete updated plan markdown...
  {PLAN_UPDATE_END}
  The coordinator will persist the plan update.
- The plan body is append-only: update todo statuses and append dated notes,
  but never delete or rewrite existing sections. The coordinator rejects
  updates that delete sections.
- Treat SEQUENCING, DECISION, stop-the-line, and founder-acked notes in the
  plan as hard constraints.
- Set a todo to `completed` only when every done-when item is met.
- Keep the plan's `## Closeout` section current (labels `Verified:`,
  `Attempted-blocked (cause):`, `Deferred (tracked in):`, `Not claimed:`).
- Address all actionable review comments.
- Run relevant tests/checks. Report each result together with the working
  directory it ran in and the exact command.
- Optionally suggest the coordinator commit subject in stdout between:
  {COMMIT_MESSAGE_BEGIN}
  concise commit subject
  {COMMIT_MESSAGE_END}
- Do not commit. The coordinator will create the git commit after your turn.
- Do not write to `.git`.
- Do not write `.a2a` plan/review files yourself.
- Do not push.
- Do not merge.
- Immediately before the final status token, include one concise line
  beginning `{DECISION_REASON_PREFIX}` summarizing completion or the blocker.
- End with exactly `{IMPLEMENTATION_READY_TOKEN}` only when the requested
  fixes produced reviewable progress.
- If a hard constraint or operator decision prevents progress, end with
  exactly `{IMPLEMENTATION_BLOCKED_TOKEN}`.
""".strip()


def build_gh_review_prompt(goal: str, pr_url: str) -> str:
    return f"""
You are the reviewer/merge gate in an agent-to-agent workflow.

Goal:
{goal}

PR:
{pr_url}

Review the PR diff and current branch. Use GitHub CLI if available to inspect the PR.

Review duties beyond the diff itself:
- Confirm the test/CI workflow actually ran on the PR's current head SHA
  (`gh api "repos/<owner>/<repo>/actions/runs?head_sha=<sha>"`). A green
  deployment preview alone is never sufficient evidence; if CI skipped the
  SHA (e.g. after a rebase), request a re-trigger (close/reopen the PR).
- If other open PRs touch the same subsystem, state the required merge order
  and semantic-conflict risks. Never call this PR independent of them unless
  the combination was actually tested.

If changes are needed:
- Leave actionable PR comments or a clear review summary.
- Immediately before the final token, include one concise line beginning
  `{DECISION_REASON_PREFIX}` explaining the most important reason.
- End with {REVIEW_CHANGES_TOKEN}.

If the PR satisfies the goal and tests are adequate:
- Immediately before the final token, include one concise line beginning
  `{DECISION_REASON_PREFIX}` explaining why the PR is ready.
- End with exactly:
{APPROVAL_TOKEN}

Do not merge the PR.
""".strip()


def build_gh_fix_prompt(goal: str, pr_url: str, review: str, constraints: str = "") -> str:
    return f"""
You are the fixer in an agent-to-agent workflow.

Goal:
{goal}

PR:
{pr_url}

Reviewer output:
{review}

{constraints}Instructions:
- Inspect PR comments and the diff.
- Address all actionable comments.
- Run relevant tests/checks. Report each result together with the working
  directory it ran in and the exact command.
- The plan body is append-only: update todo statuses and append dated notes,
  but never delete or rewrite existing sections. Keep the `## Closeout`
  section current.
- If todo status/body should change, include a complete updated plan in stdout between:
  {PLAN_UPDATE_BEGIN}
  ...complete updated plan markdown...
  {PLAN_UPDATE_END}
  The coordinator will persist the plan update.
- Optionally suggest the coordinator commit subject in stdout between:
  {COMMIT_MESSAGE_BEGIN}
  concise commit subject
  {COMMIT_MESSAGE_END}
- Do not commit or push. The coordinator will create the git commit and push after your turn.
- Do not write to `.git`.
- Do not write `.a2a` plan/review files yourself.
- Do not merge.
- Immediately before the final status token, include one concise line
  beginning `{DECISION_REASON_PREFIX}` summarizing completion or the blocker.
- End with exactly `{IMPLEMENTATION_READY_TOKEN}` only when the requested
  fixes produced reviewable progress.
- If a hard constraint or operator decision prevents progress, end with
  exactly `{IMPLEMENTATION_BLOCKED_TOKEN}`.
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
            trace.event(f"planning starts: {state.planner} returns plan for {plan_rel}")
            plan_output = run_agent(
                state.planner,
                "write implementation plan",
                build_plan_prompt(state.goal, state.base, plan_rel, conventions_note(repo)),
                repo,
                dry_run,
                log,
                trace,
                state,
                artifact=plan_rel,
            )
            persist_plan_markdown(
                plan_path,
                plan_output,
                dry_run,
                trace,
                "planner stdout",
                tokens={PLAN_READY_TOKEN},
            )
            if not sync_source_plan(repo, plan_path, state, dry_run, trace):
                return read_if_present(plan_path, "DRY_RUN_PLAN")
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

    if not sync_source_plan(repo, plan_path, state, dry_run, trace):
        return read_if_present(plan_path, "DRY_RUN_PLAN")

    if state.skip_plan_review:
        trace.event("plan review skipped")
        state.phase = "plan_ready"
        save_state(repo, state, dry_run, trace)
        return read_if_present(plan_path, "DRY_RUN_PLAN")

    for round_index in range(state.plan_review_round, state.max_plan_rounds + 1):
        trace.event(f"plan review round {round_index}/{state.max_plan_rounds}")
        review_output = run_agent(
            state.implementer,
            "review and enhance plan",
            build_plan_review_prompt(state.goal, state.base, plan_rel, conventions_note(repo)),
            repo,
            dry_run,
            log,
            trace,
            state,
            artifact=plan_rel,
        )
        if dry_run:
            review_output = (
                f"{PLAN_APPEND_BEGIN}\n## Dry-run plan review\n\nNo changes persisted.\n"
                f"{PLAN_APPEND_END}\n{DECISION_REASON_PREFIX} dry-run plan review\n"
                f"{PLAN_REVIEW_READY_TOKEN}"
            )
        review_reason = extract_decision_reason(review_output)
        if not ends_with_token(review_output, PLAN_REVIEW_READY_TOKEN) or not review_reason:
            mark_run_blocked(
                repo,
                state,
                dry_run,
                trace,
                f"Plan round {round_index}: implementer review",
                "plan review omitted the required status token or A2A_REASON line",
                "plan_written",
            )
            return read_if_present(plan_path, "DRY_RUN_PLAN")
        persist_plan_revision(
            plan_path,
            review_output,
            dry_run,
            trace,
            "implementer plan review stdout",
            tokens={PLAN_REVIEW_READY_TOKEN},
        )
        if not sync_source_plan(repo, plan_path, state, dry_run, trace):
            return read_if_present(plan_path, "DRY_RUN_PLAN")
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
        if not sync_source_plan(repo, plan_path, state, dry_run, trace):
            return read_if_present(plan_path, "DRY_RUN_PLAN")
        if dry_run:
            approval = f"{DECISION_REASON_PREFIX} dry-run approval\n{PLAN_APPROVAL_TOKEN}"
        approval_reason = extract_decision_reason(approval)
        if not approval_reason:
            mark_run_blocked(
                repo,
                state,
                dry_run,
                trace,
                f"Plan round {round_index}: reviewer decision",
                "reviewer response omitted the required A2A_REASON line",
                "plan_written",
            )
            return read_if_present(plan_path, "DRY_RUN_PLAN")
        if ends_with_token(approval, PLAN_BLOCKED_TOKEN):
            mark_run_blocked(
                repo,
                state,
                dry_run,
                trace,
                f"Plan round {round_index}: reviewer decision",
                approval_reason,
                "plan_written",
            )
            return read_if_present(plan_path, "DRY_RUN_PLAN")
        if ends_with_token(approval, PLAN_APPROVAL_TOKEN):
            append_decision(
                repo,
                state,
                dry_run,
                trace,
                f"Plan round {round_index}: approved",
                [
                    ("Reviewer", state.reviewer),
                    ("Reason", approval_reason),
                    ("Plan", plan_rel),
                ],
            )
            state.phase = "plan_ready"
            save_state(repo, state, dry_run, trace)
            return read_if_present(plan_path, "DRY_RUN_PLAN")
        if ends_with_token(approval, PLAN_CHANGES_TOKEN):
            append_decision(
                repo,
                state,
                dry_run,
                trace,
                f"Plan round {round_index}: changes requested",
                [
                    ("Reviewer", state.reviewer),
                    ("Reason", approval_reason),
                    ("Implementer", state.implementer),
                    ("Plan", plan_rel),
                ],
            )
            persist_plan_revision(
                plan_path,
                approval,
                dry_run,
                trace,
                "reviewer plan follow-up stdout",
                tokens={PLAN_CHANGES_TOKEN},
            )
            if not sync_source_plan(repo, plan_path, state, dry_run, trace):
                return read_if_present(plan_path, "DRY_RUN_PLAN")
        elif not ends_with_token(approval, PLAN_CHANGES_TOKEN):
            mark_run_blocked(
                repo,
                state,
                dry_run,
                trace,
                f"Plan round {round_index}: reviewer decision",
                "reviewer response omitted the required approval or changes-requested token",
                "plan_written",
            )
            return read_if_present(plan_path, "DRY_RUN_PLAN")
        state.plan_review_round = round_index + 1
        save_state(repo, state, dry_run, trace)

    mark_run_blocked(
        repo,
        state,
        dry_run,
        trace,
        "Plan review",
        f"plan review budget exhausted after {state.max_plan_rounds} rounds",
        "plan_written",
    )
    return read_if_present(plan_path, "DRY_RUN_PLAN")


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
        resume_pending_fix = (
            state.phase == "local_fix_pending"
            and state.pending_review_round == round_index
            and bool(state.pending_review_path)
        )
        review_path = (
            resolve_repo_path(repo, pathlib.Path(state.pending_review_path))
            if resume_pending_fix
            else review_dir / f"review-{round_index}.md"
        )
        review_rel = repo_relative(repo, review_path)
        if resume_pending_fix:
            trace.event(f"resuming pending local fix from: {review_rel}")
            review = read_if_present(review_path)
            if not review.strip():
                mark_run_blocked(
                    repo,
                    state,
                    dry_run,
                    trace,
                    f"Local fix round {round_index}",
                    f"pending review file is missing or empty: {review_rel}",
                    "local_fix_pending",
                )
                return False
        else:
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
                review = f"{DECISION_REASON_PREFIX} dry-run approval\n{APPROVAL_TOKEN}"
            review_reason = extract_decision_reason(review)
            if not review_reason:
                mark_run_blocked(
                    repo,
                    state,
                    dry_run,
                    trace,
                    f"Local review round {round_index}",
                    "reviewer response omitted the required A2A_REASON line",
                    "implementation_ready",
                )
                return False
            if ends_with_token(review, APPROVAL_TOKEN):
                trace.event(f"review approved by {state.reviewer}")
                append_decision(
                    repo,
                    state,
                    dry_run,
                    trace,
                    f"Local review round {round_index}: approved",
                    [
                        ("Reviewer", state.reviewer),
                        ("Reason", review_reason),
                        ("Review", review_rel),
                    ],
                )
                state.phase = "approved"
                state.approved = True
                state.final_review_path = review_rel
                state.pending_review_path = ""
                state.pending_review_round = 0
                save_state(repo, state, dry_run, trace)
                return True

            if not ends_with_token(review, REVIEW_CHANGES_TOKEN):
                mark_run_blocked(
                    repo,
                    state,
                    dry_run,
                    trace,
                    f"Local review round {round_index}",
                    "reviewer response omitted the required approval or changes-requested token",
                    "implementation_ready",
                )
                return False

            append_decision(
                repo,
                state,
                dry_run,
                trace,
                f"Local review round {round_index}: changes requested",
                [
                    ("Reviewer", state.reviewer),
                    ("Reason", review_reason),
                    ("Review", review_rel),
                    ("Next", f"{state.implementer} fixes local diff"),
                ],
            )
            state.phase = "local_fix_pending"
            state.pending_review_path = review_rel
            state.pending_review_round = round_index
            save_state(repo, state, dry_run, trace)

        fix_output = run_agent(
            state.implementer,
            "fix local review comments",
            build_local_fix_prompt(
                state.goal,
                state.base,
                plan_rel,
                review_rel,
                constraints_block(read_if_present(plan_path)),
            ),
            repo,
            dry_run,
            log,
            trace,
            state,
            artifact=review_rel,
        )
        if dry_run:
            fix_output = f"{DECISION_REASON_PREFIX} dry-run fix\n{IMPLEMENTATION_READY_TOKEN}"
        persist_plan_update_block(plan_path, fix_output, dry_run, trace, f"local fix round {round_index} stdout")
        if not sync_source_plan(repo, plan_path, state, dry_run, trace):
            return False
        status = "ready" if dry_run else implementation_status(fix_output)
        reason = extract_decision_reason(fix_output)
        if status != "ready" or not reason:
            reason = reason or (
                "fixer response omitted the required A2A_REASON line: "
                + summarize_agent_output(fix_output)
            )
            if status == "missing":
                reason = "fixer response omitted the required implementation status token: " + reason
            mark_run_blocked(
                repo,
                state,
                dry_run,
                trace,
                f"Local fix round {round_index}",
                reason,
                "local_fix_pending",
            )
            return False
        commit_hash = commit_if_changed(
            repo,
            commit_message_from_output(
                fix_output,
                f"A2A review fixes round {round_index}: {short_commit_goal(state.goal)}",
            ),
            dry_run,
            log,
            trace,
        )
        if not commit_hash:
            mark_run_blocked(
                repo,
                state,
                dry_run,
                trace,
                f"Local fix round {round_index}",
                "review requested changes, but the fixer produced no committable progress",
                "local_fix_pending",
            )
            return False
        append_decision(
            repo,
            state,
            dry_run,
            trace,
            f"Local fix round {round_index}: implemented",
            [
                ("Implementer", state.implementer),
                ("Response", decision_reason(fix_output)),
                ("Commit", commit_hash),
            ],
        )
        state.phase = "implementation_ready"
        state.local_review_round = round_index + 1
        state.pending_review_path = ""
        state.pending_review_round = 0
        save_state(repo, state, dry_run, trace)

    return False


def run_gh_review_loop(
    repo: pathlib.Path,
    dry_run: bool,
    log: pathlib.Path,
    trace: WorkflowTrace,
    state: RunState,
) -> bool:
    review_dir = repo / ".a2a" / "reviews" / state.run_id
    if not dry_run:
        review_dir.mkdir(parents=True, exist_ok=True)
    for round_index in range(state.gh_review_round, state.max_rounds + 1):
        resume_pending_fix = (
            state.phase == "gh_fix_pending"
            and state.pending_review_round == round_index
            and bool(state.pending_review_path)
        )
        review_path = (
            resolve_repo_path(repo, pathlib.Path(state.pending_review_path))
            if resume_pending_fix
            else review_dir / f"gh-review-{round_index}.md"
        )
        review_rel = repo_relative(repo, review_path)
        if resume_pending_fix:
            trace.event(f"resuming pending GitHub fix from: {review_rel}")
            review = read_if_present(review_path)
            if not review.strip():
                mark_run_blocked(
                    repo,
                    state,
                    dry_run,
                    trace,
                    f"GitHub fix round {round_index}",
                    f"pending review file is missing or empty: {review_rel}",
                    "gh_fix_pending",
                )
                return False
        else:
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
            persist_review_output(review_path, review, dry_run, trace)
            if dry_run:
                review = f"{DECISION_REASON_PREFIX} dry-run approval\n{APPROVAL_TOKEN}"
            review_reason = extract_decision_reason(review)
            if not review_reason:
                mark_run_blocked(
                    repo,
                    state,
                    dry_run,
                    trace,
                    f"GitHub review round {round_index}",
                    "reviewer response omitted the required A2A_REASON line",
                    "pr_ready",
                )
                return False
            if ends_with_token(review, APPROVAL_TOKEN):
                trace.event(f"GitHub review approved by {state.reviewer}")
                append_decision(
                    repo,
                    state,
                    dry_run,
                    trace,
                    f"GitHub review round {round_index}: approved",
                    [
                        ("Reviewer", state.reviewer),
                        ("Reason", review_reason),
                        ("PR", state.pr_url),
                        ("Review", review_rel),
                    ],
                )
                state.phase = "approved"
                state.approved = True
                state.pending_review_path = ""
                state.pending_review_round = 0
                save_state(repo, state, dry_run, trace)
                return True

            if not ends_with_token(review, REVIEW_CHANGES_TOKEN):
                mark_run_blocked(
                    repo,
                    state,
                    dry_run,
                    trace,
                    f"GitHub review round {round_index}",
                    "reviewer response omitted the required approval or changes-requested token",
                    "pr_ready",
                )
                return False

            append_decision(
                repo,
                state,
                dry_run,
                trace,
                f"GitHub review round {round_index}: changes requested",
                [
                    ("Reviewer", state.reviewer),
                    ("Reason", review_reason),
                    ("PR", state.pr_url),
                    ("Review", review_rel),
                    ("Next", f"{state.implementer} fixes PR comments"),
                ],
            )
            state.phase = "gh_fix_pending"
            state.pending_review_path = review_rel
            state.pending_review_round = round_index
            save_state(repo, state, dry_run, trace)

        plan_path = resolve_repo_path(repo, pathlib.Path(state.plan_path))
        fix_output = run_agent(
            state.implementer,
            "fix GitHub review comments",
            build_gh_fix_prompt(
                state.goal,
                state.pr_url,
                review,
                constraints_block(read_if_present(plan_path)),
            ),
            repo,
            dry_run,
            log,
            trace,
            state,
            artifact=state.pr_url,
        )
        if dry_run:
            fix_output = f"{DECISION_REASON_PREFIX} dry-run fix\n{IMPLEMENTATION_READY_TOKEN}"
        persist_plan_update_block(plan_path, fix_output, dry_run, trace, f"GitHub fix round {round_index} stdout")
        if not sync_source_plan(repo, plan_path, state, dry_run, trace):
            return False
        status = "ready" if dry_run else implementation_status(fix_output)
        reason = extract_decision_reason(fix_output)
        if status != "ready" or not reason:
            reason = reason or (
                "fixer response omitted the required A2A_REASON line: "
                + summarize_agent_output(fix_output)
            )
            if status == "missing":
                reason = "fixer response omitted the required implementation status token: " + reason
            mark_run_blocked(
                repo,
                state,
                dry_run,
                trace,
                f"GitHub fix round {round_index}",
                reason,
                "gh_fix_pending",
            )
            return False
        commit_hash = commit_if_changed(
            repo,
            commit_message_from_output(
                fix_output,
                f"A2A GitHub review fixes round {round_index}: {short_commit_goal(state.goal)}",
            ),
            dry_run,
            log,
            trace,
        )
        if not commit_hash:
            mark_run_blocked(
                repo,
                state,
                dry_run,
                trace,
                f"GitHub fix round {round_index}",
                "review requested changes, but the fixer produced no committable progress",
                "gh_fix_pending",
            )
            return False
        append_decision(
            repo,
            state,
            dry_run,
            trace,
            f"GitHub fix round {round_index}: implemented",
            [
                ("Implementer", state.implementer),
                ("Response", decision_reason(fix_output)),
                ("Commit", commit_hash),
            ],
        )
        push_result = run(["git", "push"], cwd=repo, dry_run=dry_run, log_file=log)
        require_ok(push_result, "push coordinator fixes")
        gh_text(
            repo,
            ["pr", "comment", state.pr_url, "--body", f"Implementer pushed fixes for round {round_index}."],
            dry_run,
            log,
        )
        state.phase = "pr_ready"
        state.gh_review_round = round_index + 1
        state.pending_review_path = ""
        state.pending_review_round = 0
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
    default_verbose = env_flag("A2A_VERBOSE")

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
    parser.add_argument(
        "--resume",
        nargs="?",
        const=LATEST_RESUME,
        help=(
            "Resume a checkpoint from .a2a/runs/<id>/state.json, or pass a state path. "
            "With no value, resumes the newest .a2a/runs/* checkpoint."
        ),
    )
    parser.add_argument(
        "--retry-blocked",
        action="store_true",
        help="Retry a blocked phase after its blocker has been resolved. Requires --resume.",
    )
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
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=default_verbose,
        help="Show summarized live agent text, tool events, stderr, and post-turn diffstats. Can also be set with A2A_VERBOSE=1.",
    )
    parser.add_argument(
        "--no-verbose",
        action="store_true",
        help="Disable verbose output on resume, even if the checkpoint or A2A_VERBOSE enabled it.",
    )
    parser.add_argument("--merge", action="store_true", help="Squash merge after reviewer approval.")
    parser.add_argument(
        "--claude-use-api-key",
        action="store_true",
        default=default_claude_use_api_key,
        help="Let claude inherit ANTHROPIC_* API-key auth instead of using claude.ai login/subscription auth.",
    )
    args = parser.parse_args()
    if args.retry_blocked and args.resume is None:
        raise SystemExit("--retry-blocked requires --resume")
    args.codex_effort = normalize_codex_effort(args.codex_effort)
    # argparse choices do not validate defaults, so an effort injected via
    # A2A_CLAUDE_EFFORT is checked here on the resolved value.
    if args.claude_effort and args.claude_effort not in CLAUDE_EFFORTS:
        raise SystemExit("Claude effort must be one of: " + ", ".join(CLAUDE_EFFORTS))
    if args.merge and args.resume is None and args.implementer == args.reviewer:
        raise SystemExit(
            "--merge requires distinct --implementer and --reviewer agents "
            "(self-merge guard): the author of a change must not be its only "
            "merge gate. Run without --merge and merge manually instead."
        )
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

    resume_state_migrated = False
    if args.resume is not None:
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
        if args.no_verbose:
            state.verbose = False
            overrides.append("verbose=false")
        elif arg_was_passed("--verbose") or default_verbose:
            state.verbose = True
            overrides.append("verbose=true")
        plan_path = resolve_repo_path(repo, pathlib.Path(state.plan_path))
        create_plan = False
        trace.event(f"resuming run {state.run_id} from {repo_relative(repo, loaded_path)}")
        if state.verbose and not any(
            [arg_was_passed("--verbose"), arg_was_passed("--no-verbose"), default_verbose]
        ):
            trace.event("resume setting: verbose=true from checkpoint; pass --no-verbose to disable")
        for override in overrides:
            trace.event(f"resume override: {override}")
        migrated_review = migrate_legacy_pending_fix(repo, state)
        if migrated_review:
            resume_state_migrated = True
            trace.event(f"resume migration: pending local fix recovered from {migrated_review}")
    else:
        repo = initial_repo
        if not args.goal and not args.plan:
            raise SystemExit("Either --goal, --plan, or --resume is required.")

        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        log_dir = repo / ".a2a" / "logs" / stamp
        log = log_dir / "run.log"
        trace = WorkflowTrace(log)
        goal = args.goal
        source_plan_path = None
        plan_slug = ""
        if args.plan:
            plan_path = resolve_repo_path(repo, args.plan)
            if not plan_path.exists():
                raise SystemExit(f"Plan does not exist: {plan_path}")
            goal = goal or f"Execute plan {repo_relative(repo, plan_path)}"
            source_plan_path = repo_relative(repo, plan_path)
            plan_slug = slugify_plan_name(plan_path)
            plan_path = materialize_working_plan(repo, plan_path, stamp, args.dry_run, trace)
            create_plan = False
        else:
            assert goal is not None
            plan_slug = slugify_goal(goal)
            # Namespaced by run id so runs with similar goals never share a
            # plan ledger.
            plan_path = repo / ".a2a" / "plans" / f"{stamp}-{plan_slug}.plan.md"
            create_plan = True
        branch = args.branch or default_branch_name(plan_slug, stamp)
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
            verbose=args.verbose,
            log_path=str(log),
            decision_log_path=repo_relative(repo, decision_log_path(repo, stamp)),
            source_plan_sha256=(
                plan_digest(plan_path.read_text(encoding="utf-8"))
                if source_plan_path and plan_path.exists() and not args.dry_run
                else ""
            ),
        )

    if args.resume is not None and state.phase == "blocked":
        if not args.retry_blocked:
            return blocked_exit(state, log, trace)
        if not state.blocked_resume_phase:
            raise SystemExit(
                "Blocked checkpoint has no resume phase. Inspect its decisions.md and state.json before retrying."
            )
        retry_phase = state.blocked_resume_phase
        trace.event(f"retrying previously blocked phase: {retry_phase}")
        state.phase = retry_phase
        state.blocked_reason = ""
        state.blocked_resume_phase = ""

    ensure_a2a_dirs(repo, args.dry_run)
    trace.event(f"repo: {repo}")
    trace_capability_manifest(repo, args.dry_run, log, trace)
    if state.verbose:
        trace.event("verbose output: summarized agent text, tool events, stderr, and post-turn diffstats")
    trace.event(f"Codex auth status: {codex_auth_status(repo, args.dry_run, log)}")
    trace.event(
        "Claude auth: "
        + ("API key env inherited" if state.claude_use_api_key else "claude.ai login/subscription")
    )
    if state.claude_use_api_key:
        trace.event("Claude auth status: skipped for API-key mode")
    else:
        trace.event(f"Claude auth status: {claude_auth_status(repo, args.dry_run, log)}")
    trace_run_defaults(repo, plan_path, state, trace)
    if args.resume is not None:
        trace.event(f"branch checkout: {state.branch}")
        checkout_branch(repo, state.branch, args.dry_run, log)
        if args.retry_blocked or resume_state_migrated:
            save_state(repo, state, args.dry_run, trace)
    else:
        trace.event(f"branch setup: {state.branch}")
        ensure_branch(repo, state.branch, state.base, args.dry_run, log, trace)
        ensure_gitignore(repo, args.dry_run, trace)
        save_state(repo, state, args.dry_run, trace)

    negotiate_plan(
        repo,
        plan_path,
        args.dry_run,
        log,
        trace,
        state,
        create_plan=create_plan,
    )
    if state.phase == "blocked":
        return blocked_exit(state, log, trace)

    if state.phase == "plan_ready":
        implement_output = run_agent(
            state.implementer,
            "implement approved plan",
            build_implement_prompt(
                state.goal,
                repo_relative(repo, plan_path),
                state.base,
                conventions_note(repo),
                constraints_block(read_if_present(plan_path)),
            ),
            repo,
            args.dry_run,
            log,
            trace,
            state,
            artifact=repo_relative(repo, plan_path),
        )
        persist_plan_update_block(plan_path, implement_output, args.dry_run, trace, "implementation stdout")
        if not sync_source_plan(repo, plan_path, state, args.dry_run, trace):
            return blocked_exit(state, log, trace)
        status = "ready" if args.dry_run else implementation_status(implement_output)
        reason = "dry-run implementation" if args.dry_run else extract_decision_reason(implement_output)
        if status != "ready" or not reason:
            reason = reason or (
                "implementer response omitted the required A2A_REASON line: "
                + summarize_agent_output(implement_output)
            )
            if status == "missing":
                reason = "implementer response omitted the required implementation status token: " + reason
            mark_run_blocked(
                repo,
                state,
                args.dry_run,
                trace,
                "Implementation",
                reason,
                "plan_ready",
            )
            return blocked_exit(state, log, trace)
        commit_hash = commit_if_changed(
            repo,
            commit_message_from_output(
                implement_output,
                f"A2A implementation: {short_commit_goal(state.goal)}",
            ),
            args.dry_run,
            log,
            trace,
        )
        if not commit_hash:
            mark_run_blocked(
                repo,
                state,
                args.dry_run,
                trace,
                "Implementation",
                "implementer reported completion but produced no committable progress",
                "plan_ready",
            )
            return blocked_exit(state, log, trace)
        append_decision(
            repo,
            state,
            args.dry_run,
            trace,
            "Implementation: completed",
            [
                ("Implementer", state.implementer),
                ("Response", decision_reason(implement_output)),
                ("Commit", commit_hash),
            ],
        )
        state.phase = "implementation_ready"
        save_state(repo, state, args.dry_run, trace)
    elif state.phase == "implementation_ready":
        trace.event("implementation already ready; resuming review")
    elif state.phase in ("local_fix_pending", "gh_fix_pending", "approved", "pr_ready", "done"):
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
            commit_hash = commit_if_changed(
                repo,
                f"A2A final changes: {short_commit_goal(state.goal)}",
                args.dry_run,
                log,
                trace,
            )
            if commit_hash:
                append_decision(
                    repo,
                    state,
                    args.dry_run,
                    trace,
                    "Final changes: committed before GitHub review",
                    [("Commit", commit_hash)],
                )
            state.pr_url = open_or_update_pr(repo, state, plan_path, args.dry_run, log)
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
                commit_hash = commit_if_changed(
                    repo,
                    f"A2A final changes: {short_commit_goal(state.goal)}",
                    args.dry_run,
                    log,
                    trace,
                )
                if commit_hash:
                    append_decision(
                        repo,
                        state,
                        args.dry_run,
                        trace,
                        "Final changes: committed before PR",
                        [("Commit", commit_hash)],
                    )
                state.pr_url = open_or_update_pr(repo, state, plan_path, args.dry_run, log)
                save_state(repo, state, args.dry_run, trace)

    if not approved:
        if state.phase == "blocked":
            return blocked_exit(state, log, trace)
        pr_note = f" PR: {state.pr_url}" if state.pr_url else ""
        trace.event(f"not approved within {state.max_rounds} review rounds.{pr_note}")
        trace.event(f"resume with: a2a-loop --resume {state.run_id}")
        trace.event(f"logs: {log}")
        return 2

    trace.event(f"approved by {state.reviewer}. PR: {state.pr_url}")
    if state.merge and state.implementer == state.reviewer:
        trace.event(
            "merge refused: implementer and reviewer are the same agent "
            "(self-merge guard); merge manually or rerun with distinct roles"
        )
        state.merge = False
    if state.merge:
        # CI must have run on the PR's current head SHA; a rebase or filtered
        # workflow can leave only a deployment preview green, which is not
        # evidence the change was tested.
        checks = run(["gh", "pr", "checks", state.pr_url], cwd=repo, dry_run=args.dry_run, log_file=log)
        checks_output = (checks.stdout + checks.stderr).lower()
        if checks.returncode != 0 and "no checks reported" not in checks_output:
            trace.event(
                "merge blocked: PR checks are failing or still pending on the "
                "head SHA; merge manually once they pass (re-trigger skipped "
                "workflows by closing and reopening the PR)"
            )
        else:
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
