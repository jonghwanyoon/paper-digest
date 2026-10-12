"""요약 엔진: Codex CLI / Claude Code CLI 를 구독 인증으로 비대화식 호출.

둘 다 빈 임시 디렉터리에서, 프롬프트는 stdin 으로, 출력은 JSON 스키마로 받는다.
- codex : 사용자 config 무시(MCP·플러그인 차단), read-only 샌드박스, shell 도구·웹 검색 비활성화.
          그래도 도구 호출 이벤트가 보이면 결과를 버린다.
- claude: --tools "" 로 모든 내장 도구 제거, MCP·설정 파일 미로드.
API 키 환경변수는 자식 프로세스에 넘기지 않는다(구독 인증만 사용).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from digest.config import EngineSettings


class QuotaExhausted(Exception):
    """구독 사용량 한도. 이번 실행을 멈추고 남은 논문은 다음 실행으로 넘긴다."""


class EngineUnavailable(Exception):
    """인증 만료·CLI 없음 등 논문과 무관한 실패. 시도 횟수를 올리지 않고 멈춘다."""


class EngineError(Exception):
    """이 논문 한 편의 실패(형식 오류, 시간 초과, 도구 사용 시도 등)."""


Engine = Callable[[str, dict], dict]

CODEX_QUOTA = re.compile(r"usage_limit_exceeded|hit your usage limit|usage limit", re.I)
CLAUDE_QUOTA = re.compile(
    r"hit your (?:\w+ )?(?:session |weekly |usage |monthly |spend )?limit|usage limit|limit reached"
    r"|temporarily limiting requests|Request rejected \(429\)|rate_limit", re.I)
AUTH_FAIL = re.compile(
    r"Failed to authenticate|not logged in|log ?in required|please (?:run|use) .{0,20}login"
    r"|401 Unauthorized|refresh token|OAuth session expired|authentication", re.I)

_FORBIDDEN_CODEX_ITEMS = {"command_execution", "mcp_tool_call", "web_search", "file_change"}
_STRIP_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}


def _classify_failure(message: str, quota: re.Pattern) -> Exception:
    if quota.search(message):
        return QuotaExhausted(message.strip()[:500])
    if AUTH_FAIL.search(message):
        return EngineUnavailable(message.strip()[:500])
    return EngineError(message.strip()[:1000])


def codex_command(s: EngineSettings, workdir: Path, schema_path: Path, out_path: Path) -> list[str]:
    cmd = [
        "codex", "exec",
        "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
        "-s", "read-only", "-C", str(workdir),
        "-c", 'approval_policy="never"',
        "-c", 'web_search="disabled"',
        "--disable", "shell_tool", "--disable", "unified_exec",
        "--json", "--output-schema", str(schema_path), "-o", str(out_path),
    ]
    if s.model:
        cmd += ["-m", s.model]
    if s.effort:
        cmd += ["-c", f'model_reasoning_effort="{s.effort}"']
    return cmd + ["-"]


def parse_codex_events(stdout: str) -> tuple[bool, list[str], list[str]]:
    """(turn.failed 여부, 오류 메시지들, 금지된 도구 사용 항목 유형들).

    최상위 error 이벤트는 재연결 같은 일시 오류일 수 있으므로 turn.failed/종료코드로 실패를 판정하고,
    메시지는 실패 원인 분류에만 쓴다.
    """
    turn_failed, messages, tools = False, [], []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "turn.failed":
            turn_failed = True
            messages.append(str((ev.get("error") or {}).get("message", ev)))
        elif t == "error":
            messages.append(str(ev.get("message", ev)))
        elif t and t.startswith("item."):
            itype = (ev.get("item") or {}).get("type")
            if itype in _FORBIDDEN_CODEX_ITEMS:
                tools.append(itype)
    return turn_failed, messages, tools


def run_codex(prompt: str, schema: dict, s: EngineSettings) -> dict:
    with tempfile.TemporaryDirectory(prefix="digest-codex-") as tmp:
        work = Path(tmp) / "work"
        work.mkdir()
        schema_path = Path(tmp) / "schema.json"
        out_path = Path(tmp) / "out.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        try:
            proc = subprocess.run(codex_command(s, work, schema_path, out_path), input=prompt,
                                  capture_output=True, text=True, timeout=s.timeout_sec,
                                  cwd=work, env=_child_env())
        except FileNotFoundError as e:
            raise EngineUnavailable("codex CLI 를 찾을 수 없음 (PATH 확인)") from e
        except subprocess.TimeoutExpired as e:
            raise EngineError(f"codex 시간 초과({s.timeout_sec}s)") from e
        turn_failed, messages, tools = parse_codex_events(proc.stdout)
        if turn_failed or proc.returncode != 0:
            raise _classify_failure("\n".join(messages) + "\n" + proc.stderr[-2000:], CODEX_QUOTA)
        if tools:
            raise EngineError(f"금지된 도구 사용 시도: {sorted(set(tools))} — 출력 폐기")
        if not out_path.exists():
            raise EngineError("codex 가 최종 메시지를 남기지 않음")
        try:
            return json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise EngineError(f"codex 출력 JSON 파싱 실패: {e}") from e


def claude_command(s: EngineSettings, schema: dict) -> list[str]:
    cmd = [
        "claude", "-p",
        "--tools", "",
        "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format", "json", "--json-schema", json.dumps(schema),
    ]
    if s.model:
        cmd += ["--model", s.model]
    if s.effort:
        cmd += ["--effort", s.effort]
    return cmd


def run_claude(prompt: str, schema: dict, s: EngineSettings) -> dict:
    with tempfile.TemporaryDirectory(prefix="digest-claude-") as tmp:
        try:
            proc = subprocess.run(claude_command(s, schema), input=prompt, capture_output=True,
                                  text=True, timeout=s.timeout_sec, cwd=tmp, env=_child_env())
        except FileNotFoundError as e:
            raise EngineUnavailable("claude CLI 를 찾을 수 없음 (PATH 확인)") from e
        except subprocess.TimeoutExpired as e:
            raise EngineError(f"claude 시간 초과({s.timeout_sec}s)") from e
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise _classify_failure(proc.stdout[-1000:] + "\n" + proc.stderr[-2000:], CLAUDE_QUOTA)
    if result.get("is_error") or proc.returncode != 0:
        raise _classify_failure(str(result.get("result", "")) + "\n" + proc.stderr[-1000:], CLAUDE_QUOTA)
    out = result.get("structured_output")
    if isinstance(out, dict):
        return out
    try:
        return json.loads(result.get("result", ""))
    except (json.JSONDecodeError, TypeError) as e:
        raise EngineError("claude 가 구조화 출력을 돌려주지 않음") from e


def get_engine(s: EngineSettings) -> Engine:
    runners = {"codex": run_codex, "claude": run_claude}
    if s.engine not in runners:
        raise ValueError(f"알 수 없는 엔진: {s.engine} (codex | claude)")
    runner = runners[s.engine]
    return lambda prompt, schema: runner(prompt, schema, s)
