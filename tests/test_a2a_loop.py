"""Unit tests for the pure helpers in a2a-loop.py.

Run with: python3 -m unittest discover tests
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("a2a_loop", ROOT / "a2a-loop.py")
a2a = importlib.util.module_from_spec(_spec)
sys.modules["a2a_loop"] = a2a
_spec.loader.exec_module(a2a)


class SlugifyGoalTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(a2a.slugify_goal("Add login page"), "add-login-page")

    def test_symbols_collapse(self):
        self.assertEqual(a2a.slugify_goal("Fix: bug #42 (again)"), "fix-bug-42-again")

    def test_truncated_to_64(self):
        slug = a2a.slugify_goal("x" * 200)
        self.assertLessEqual(len(slug), 64)

    def test_empty_falls_back(self):
        self.assertEqual(a2a.slugify_goal("!!!"), "a2a-plan")


class BranchNameTests(unittest.TestCase):
    def test_plan_name_strips_plan_md_suffix(self):
        self.assertEqual(a2a.slugify_plan_name(pathlib.Path("phase-9.plan.md")), "phase-9")

    def test_plan_name_falls_back_to_stem(self):
        self.assertEqual(a2a.slugify_plan_name(pathlib.Path("docs/blueprint.md")), "blueprint")

    def test_default_branch_uses_plan_slug_and_date(self):
        branch = a2a.default_branch_name("phase-9", "20260707-052409-881334")
        self.assertEqual(branch, "a2a/phase-9-20260707")


class EnsureBranchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        subprocess.run(["git", "init"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "file.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "file.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        self.initial_branch = self.git("branch", "--show-current")
        self.log = self.repo / "run.log"

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.repo, check=True, text=True, stdout=subprocess.PIPE)
        return result.stdout.strip()

    def test_existing_branch_is_not_reset(self):
        a2a.ensure_branch(self.repo, "a2a/existing", dry_run=False, log=self.log)
        branch_head = self.git("rev-parse", "HEAD")
        subprocess.run(["git", "checkout", self.initial_branch], cwd=self.repo, check=True, stdout=subprocess.PIPE)
        (self.repo / "file.txt").write_text("main moved\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "main moved"], cwd=self.repo, check=True, stdout=subprocess.PIPE)

        a2a.ensure_branch(self.repo, "a2a/existing", dry_run=False, log=self.log)

        self.assertEqual(self.git("branch", "--show-current"), "a2a/existing")
        self.assertEqual(self.git("rev-parse", "HEAD"), branch_head)


class NormalizeCodexEffortTests(unittest.TestCase):
    def test_none_passthrough(self):
        self.assertIsNone(a2a.normalize_codex_effort(None))

    def test_valid_passthrough(self):
        self.assertEqual(a2a.normalize_codex_effort("medium"), "medium")

    def test_aliases_map_to_high(self):
        for alias in ("extra-high", "xhigh", "max"):
            self.assertEqual(a2a.normalize_codex_effort(alias), "high")

    def test_invalid_raises(self):
        with self.assertRaises(SystemExit):
            a2a.normalize_codex_effort("turbo")


class SanitizeDisplayValueTests(unittest.TestCase):
    def test_strips_ansi(self):
        self.assertEqual(a2a.sanitize_display_value("\x1b[1mgpt-5.5\x1b[0m"), "gpt-5.5")

    def test_strips_literal_bracket_suffix(self):
        self.assertEqual(a2a.sanitize_display_value("gpt-5.5[1m]"), "gpt-5.5")

    def test_non_string_is_none(self):
        self.assertIsNone(a2a.sanitize_display_value(42))

    def test_empty_is_none(self):
        self.assertIsNone(a2a.sanitize_display_value("  "))


class EndsWithTokenTests(unittest.TestCase):
    TOKEN = a2a.APPROVAL_TOKEN

    def test_exact_final_line_matches(self):
        self.assertTrue(a2a.ends_with_token(f"Looks good.\n\n{self.TOKEN}\n", self.TOKEN))

    def test_token_within_trailing_window_matches(self):
        # Tolerates CLI chrome such as codex exec usage footers.
        output = f"Review done.\n{self.TOKEN}\ntokens used: 1234\n"
        self.assertTrue(a2a.ends_with_token(output, self.TOKEN))

    def test_token_quoted_in_prose_does_not_match(self):
        output = (
            f"I was asked to end with {self.TOKEN}, but changes are needed.\n"
            "Please fix the failing tests."
        )
        self.assertFalse(a2a.ends_with_token(output, self.TOKEN))

    def test_token_far_from_end_does_not_match(self):
        lines = [self.TOKEN] + [f"finding {i}" for i in range(10)]
        self.assertFalse(a2a.ends_with_token("\n".join(lines), self.TOKEN))

    def test_empty_output_does_not_match(self):
        self.assertFalse(a2a.ends_with_token("", self.TOKEN))


class EnsureGitignoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def gitignore(self) -> str:
        return (self.repo / ".gitignore").read_text(encoding="utf-8")

    def test_creates_missing_file(self):
        a2a.ensure_gitignore(self.repo, dry_run=False)
        self.assertIn(".a2a/\n", self.gitignore())
        self.assertIn("a2a-logs/\n", self.gitignore())

    def test_idempotent(self):
        a2a.ensure_gitignore(self.repo, dry_run=False)
        first = self.gitignore()
        a2a.ensure_gitignore(self.repo, dry_run=False)
        self.assertEqual(first, self.gitignore())

    def test_respects_entries_without_trailing_slash(self):
        (self.repo / ".gitignore").write_text(".a2a\na2a-logs\n", encoding="utf-8")
        a2a.ensure_gitignore(self.repo, dry_run=False)
        self.assertEqual(self.gitignore(), ".a2a\na2a-logs\n")

    def test_appends_after_missing_trailing_newline(self):
        (self.repo / ".gitignore").write_text("node_modules", encoding="utf-8")
        a2a.ensure_gitignore(self.repo, dry_run=False)
        self.assertIn("node_modules\n.a2a/\n", self.gitignore())

    def test_dry_run_writes_nothing(self):
        a2a.ensure_gitignore(self.repo, dry_run=True)
        self.assertFalse((self.repo / ".gitignore").exists())


class ResolveStatePathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_run_id(self):
        path = a2a.resolve_state_path(self.repo, "20260706-123456-000000")
        self.assertEqual(path, self.repo / ".a2a" / "runs" / "20260706-123456-000000" / "state.json")

    def test_directory(self):
        run_dir = self.repo / "somewhere"
        run_dir.mkdir()
        self.assertEqual(a2a.resolve_state_path(self.repo, str(run_dir)), run_dir / "state.json")

    def test_relative_json_path(self):
        path = a2a.resolve_state_path(self.repo, "sub/state.json")
        self.assertEqual(path, self.repo / "sub" / "state.json")

    def test_absolute_json_path(self):
        target = self.repo / "state.json"
        self.assertEqual(a2a.resolve_state_path(self.repo, str(target)), target)


class ResolveRepoPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def test_inside_repo(self):
        path = a2a.resolve_repo_path(self.repo, pathlib.Path("plans/x.plan.md"))
        self.assertEqual(path, self.repo / "plans" / "x.plan.md")

    def test_outside_repo_raises(self):
        with self.assertRaises(SystemExit):
            a2a.resolve_repo_path(self.repo, pathlib.Path("/etc/passwd"))

    def test_traversal_outside_repo_raises(self):
        with self.assertRaises(SystemExit):
            a2a.resolve_repo_path(self.repo, pathlib.Path("../escape.md"))


class StateRoundTripTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.trace = a2a.WorkflowTrace(self.repo / "run.log")

    def make_state(self) -> a2a.RunState:
        return a2a.RunState(
            version=a2a.STATE_VERSION,
            run_id="test-run",
            repo=str(self.repo),
            branch="a2a/test",
            base="main",
            goal="test goal",
            plan_path=".a2a/plans/test.plan.md",
            source_plan_path=None,
            planner="claude",
            implementer="codex",
            reviewer="claude",
            max_plan_rounds=2,
            max_rounds=3,
            skip_plan_review=False,
            gh_review=False,
            merge=False,
            codex_model="gpt-5.5",
            codex_effort="high",
            codex_display_model="gpt-5.5",
            codex_display_effort="high",
            codex_model_source="--codex-model",
            codex_effort_source="--codex-effort",
            claude_model="claude-fable-5",
            claude_effort="high",
            claude_display_model="claude-fable-5",
            claude_display_effort="high",
            claude_model_source="--claude-model",
            claude_effort_source="--claude-effort",
            log_path=str(self.repo / "run.log"),
        )

    def test_save_then_load_round_trips(self):
        state = self.make_state()
        a2a.save_state(self.repo, state, dry_run=False, trace=self.trace)
        loaded = a2a.load_state(a2a.state_path(self.repo, state.run_id))
        self.assertEqual(loaded, state)

    def test_dry_run_saves_nothing(self):
        state = self.make_state()
        a2a.save_state(self.repo, state, dry_run=True, trace=self.trace)
        self.assertFalse(a2a.state_path(self.repo, state.run_id).exists())

    def test_missing_state_raises(self):
        with self.assertRaises(SystemExit):
            a2a.load_state(self.repo / "nope" / "state.json")

    def test_version_mismatch_raises(self):
        state = self.make_state()
        a2a.save_state(self.repo, state, dry_run=False, trace=self.trace)
        path = a2a.state_path(self.repo, state.run_id)
        path.write_text(path.read_text(encoding="utf-8").replace('"version": 1', '"version": 99'), encoding="utf-8")
        with self.assertRaises(SystemExit):
            a2a.load_state(path)


class PersistReviewOutputTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.trace = a2a.WorkflowTrace(self.repo / "run.log")
        self.review = self.repo / "reviews" / "review-1.md"

    def test_writes_when_missing(self):
        a2a.persist_review_output(self.review, "Looks good.\n", dry_run=False, trace=self.trace)
        self.assertEqual(self.review.read_text(encoding="utf-8"), "Looks good.\n")

    def test_keeps_existing_content(self):
        self.review.parent.mkdir(parents=True)
        self.review.write_text("agent-written review\n", encoding="utf-8")
        a2a.persist_review_output(self.review, "stdout fallback\n", dry_run=False, trace=self.trace)
        self.assertEqual(self.review.read_text(encoding="utf-8"), "agent-written review\n")

    def test_skips_empty_output(self):
        a2a.persist_review_output(self.review, "   \n", dry_run=False, trace=self.trace)
        self.assertFalse(self.review.exists())


class ReadIfPresentTests(unittest.TestCase):
    def test_fallback_when_missing(self):
        self.assertEqual(a2a.read_if_present(pathlib.Path("/nonexistent/file"), "fallback"), "fallback")


class AgentErrorScanTests(unittest.TestCase):
    def make_result(self, stdout: str = "", stderr: str = "") -> a2a.CmdResult:
        return a2a.CmdResult(args=["codex"], returncode=0, stdout=stdout, stderr=stderr)

    def test_fatal_pattern_in_stderr_raises(self):
        result = self.make_result(stderr="ERROR: unexpected status 400 Bad Request")
        with self.assertRaises(SystemExit):
            a2a.require_no_agent_error(result, "Codex turn")

    def test_fatal_pattern_at_stdout_tail_raises(self):
        result = self.make_result(stdout="working...\nERROR: unexpected status 500")
        with self.assertRaises(SystemExit):
            a2a.require_no_agent_error(result, "Codex turn")

    def test_pattern_quoted_deep_in_transcript_is_ignored(self):
        # A diff early in a long transcript that merely mentions the pattern
        # must not kill the run.
        transcript = "ERROR: unexpected status\n" + "\n".join(f"line {i}" for i in range(100))
        result = self.make_result(stdout=transcript)
        a2a.require_no_agent_error(result, "Codex turn")


if __name__ == "__main__":
    unittest.main()
