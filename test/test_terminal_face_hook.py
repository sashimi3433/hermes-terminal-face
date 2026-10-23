"""Tests for hooks/terminal-face-hook.py (Grok Build / Antigravity CLI).

Run with: python3 -m unittest discover -s test -p 'test_*.py'
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "terminal-face-hook.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


hook = load("terminal_face_hook", HOOK)
hermes = load("hermes_terminal_face", ROOT / "__init__.py")

OSC12 = re.compile(r"\x1b\]12;#([0-9a-f]{6})\x07")


def read_colors(path: Path) -> list[str]:
    return OSC12.findall(path.read_text())


class RampTests(unittest.TestCase):
    def test_ramp_matches_hermes_and_pi(self) -> None:
        pairs = [
            (None, hook.THINK),
            (hook.IDLE, hook.THINK),
            (hook.THINK, hook.WORK),
            (hook.WORK, hook.DONE),
            (hook.ERR, hook.DONE),
            (hook.DONE, hook.IDLE),
            (hook.THINK, hook.ERR),
            (hook.WORK, hook.ERR),
        ]
        for source, target in pairs:
            self.assertEqual(
                hook.plan_ramp(source, target),
                hermes._plan_ramp(source, target),
                f"{source} -> {target}",
            )

    def test_ramp_ends_on_key_color(self) -> None:
        steps = hook.plan_ramp(hook.THINK, hook.WORK)
        self.assertEqual(steps[-1], hook.WORK)
        self.assertEqual(len(steps), hook.RAMP_HEAD + hook.RAMP_BODY + hook.RAMP_TAIL)


class ClassificationTests(unittest.TestCase):
    def test_mutating_tools(self) -> None:
        for name in (
            "run_terminal_command",
            "search_replace",
            "write",
            "Bash",
            "run_command",
            "write_file",
            "replace_file_content",
            "browser_click_element",
        ):
            self.assertTrue(hook.is_mutating(name), name)

    def test_read_tools(self) -> None:
        for name in (
            "read_file",
            "view_file",
            "grep",
            "grep_search",
            "list_dir",
            "web_search",
            "read_url_content",
            "mcp__docs__search",
        ):
            self.assertFalse(hook.is_mutating(name), name)

    def test_unknown_tools_stay_thinking(self) -> None:
        self.assertFalse(hook.is_mutating("ask_user"))
        self.assertFalse(hook.is_mutating("mcp__linear__list_issues"))

    def test_grok_exit_code_is_an_error(self) -> None:
        payload = {"tool_name": "run_terminal_command", "tool_response": {"exit_code": 3}}
        self.assertTrue(hook.looks_like_error(payload))
        payload["tool_response"]["exit_code"] = 0
        self.assertFalse(hook.looks_like_error(payload))

    def test_agy_error_field(self) -> None:
        self.assertFalse(hook.looks_like_error({"error": ""}))
        self.assertTrue(hook.looks_like_error({"error": "tool crashed"}))


class EventMappingTests(unittest.TestCase):
    def target(self, event: str, payload: dict | None = None) -> str | None:
        args = types.SimpleNamespace(source="grok", event=event)
        return hook.target_for(args, payload or {})

    def test_simple_events(self) -> None:
        self.assertEqual(self.target("reset"), hook.IDLE)
        self.assertEqual(self.target("idle"), hook.IDLE)
        self.assertEqual(self.target("thinking"), hook.THINK)
        self.assertEqual(self.target("error"), hook.ERR)
        self.assertEqual(self.target("done"), hook.DONE)

    def test_tool_events(self) -> None:
        self.assertEqual(self.target("pre-tool", {"tool_name": "read_file"}), hook.THINK)
        self.assertEqual(
            self.target("pre-tool", {"tool_name": "run_terminal_command"}), hook.WORK
        )
        agy = types.SimpleNamespace(source="agy", event="post-tool")
        self.assertEqual(
            hook.target_for(agy, {"toolCall": {"name": "write_file"}, "error": ""}),
            hook.WORK,
        )
        self.assertEqual(
            hook.target_for(agy, {"toolCall": {"name": "write_file"}, "error": "boom"}),
            hook.ERR,
        )

    def test_subagent_and_session_end_stops_are_ignored(self) -> None:
        self.assertIsNone(self.target("done", {"subagentType": "explore"}))
        self.assertIsNone(self.target("done", {"reason": "shutdown"}))


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="terminal-face-hook-")
        self.tty = Path(self.dir) / "tty"
        self.tty.write_text("")

    def run_hook(self, event: str, payload: dict | None = None) -> float:
        env = dict(os.environ, TERMINAL_FACE_TTY=str(self.tty))
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(HOOK), "--source", "grok", "--event", event],
            input=json.dumps(payload or {}),
            text=True,
            capture_output=True,
            env=env,
            timeout=5,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", "hooks must not write to stdout")
        self.assertLess(elapsed, 1.0, "hook must return before the ramp finishes")
        return elapsed

    def wait_for_ramp(self, timeout: float = 3.0) -> list[str]:
        deadline = time.monotonic() + timeout
        last_count = -1
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.02)
            count = len(read_colors(self.tty))
            if count != last_count:
                last_count = count
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since > 0.15:
                break
        return read_colors(self.tty)

    def test_lifecycle(self) -> None:
        self.run_hook("reset")
        self.assertEqual(read_colors(self.tty)[-1], hook.IDLE)

        self.run_hook("thinking")
        colors = self.wait_for_ramp()
        self.assertEqual(colors[-1], hook.THINK)
        self.assertGreater(len(colors), 20, "state change should ramp")

        before = len(read_colors(self.tty))
        self.run_hook("thinking")
        self.assertEqual(len(read_colors(self.tty)), before, "same state is a no-op")

        self.run_hook(
            "post-tool",
            {
                "tool_name": "run_terminal_command",
                "tool_response": {"exit_code": 1},
            },
        )
        self.assertEqual(self.wait_for_ramp()[-1], hook.ERR)

        self.run_hook("done")
        self.assertEqual(self.wait_for_ramp()[-1], hook.DONE)

        self.run_hook("reset")
        self.assertEqual(read_colors(self.tty)[-1], hook.IDLE)


if __name__ == "__main__":
    unittest.main()
