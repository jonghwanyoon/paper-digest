"""가짜 codex/claude 실행 파일을 PATH 앞에 두고 엔진 래퍼의 판정을 확인한다."""

import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from digest.config import EngineSettings
from digest.engines import (EngineError, EngineUnavailable, QuotaExhausted, run_claude, run_codex)

SETTINGS = EngineSettings(engine="codex", model="m", effort="low", timeout_sec=30,
                          max_attempts=3, max_input_chars=1000)

# 가짜 codex: 시나리오에 따라 JSONL 이벤트를 찍고, -o 파일에 최종 메시지를 쓴다.
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
out = args[args.index("-o") + 1]
sys.stdin.read()
mode = os.environ["FAKE_MODE"]
def ev(**k): print(json.dumps(k))
ev(type="thread.started", thread_id="t")
if mode == "quota":
    ev(type="error", message="You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage ...")
    ev(type="turn.failed", error={"message": "You've hit your usage limit."})
    sys.exit(1)
if mode == "tool":
    ev(type="item.completed", item={"type": "command_execution", "command": "cat ~/.ssh/id_ed25519"})
if mode == "transient":
    ev(type="error", message="Reconnecting... 1/5")
if mode == "auth":
    print("Error: refresh token expired, please run codex login", file=sys.stderr)
    sys.exit(1)
open(out, "w").write(json.dumps({"ok": True, "flags": [a for a in args if a.startswith("--disable")] + args[args.index("-s"):args.index("-s") + 2]}))
ev(type="turn.completed", usage={})
'''

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
mode = os.environ["FAKE_MODE"]
tools = sys.argv[sys.argv.index("--tools") + 1]
if mode == "quota":
    print(json.dumps({"type": "result", "is_error": True, "result": "You've hit your session limit · resets 3pm"}))
    sys.exit(1)
if mode == "env":
    print(json.dumps({"type": "result", "is_error": False, "structured_output": {"key": os.environ.get("ANTHROPIC_API_KEY")}}))
    sys.exit(0)
print(json.dumps({"type": "result", "is_error": False, "structured_output": {"tools": tools}}))
'''


class FakeCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name, body in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE)):
            p = Path(self.tmp.name) / name
            p.write_text(textwrap.dedent(body))
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.tmp.name}{os.pathsep}{self.old_path}"

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        os.environ.pop("FAKE_MODE", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.tmp.cleanup()

    def run_mode(self, fn, mode):
        os.environ["FAKE_MODE"] = mode
        return fn("prompt", {"type": "object"}, SETTINGS)

    def test_codex_success_uses_sandbox_flags(self):
        out = self.run_mode(run_codex, "ok")
        self.assertEqual(out["flags"], ["--disable", "--disable", "-s", "read-only"])

    def test_codex_usage_limit_is_quota(self):
        with self.assertRaises(QuotaExhausted):
            self.run_mode(run_codex, "quota")

    def test_codex_tool_use_discards_output(self):
        with self.assertRaisesRegex(EngineError, "command_execution"):
            self.run_mode(run_codex, "tool")

    def test_codex_transient_error_event_still_succeeds(self):
        self.assertTrue(self.run_mode(run_codex, "transient")["ok"])

    def test_codex_auth_failure_is_unavailable(self):
        with self.assertRaises(EngineUnavailable):
            self.run_mode(run_codex, "auth")

    def test_claude_runs_without_tools(self):
        self.assertEqual(self.run_mode(run_claude, "ok"), {"tools": ""})

    def test_claude_session_limit_is_quota(self):
        with self.assertRaises(QuotaExhausted):
            self.run_mode(run_claude, "quota")

    def test_api_key_not_passed_to_cli(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-should-not-leak"
        self.assertEqual(self.run_mode(run_claude, "env"), {"key": None})


if __name__ == "__main__":
    unittest.main()
