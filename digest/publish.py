"""data/ 변경분을 커밋하고 push. GitHub Actions 가 push 를 받아 사이트를 빌드·배포한다."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from digest.config import Config

log = logging.getLogger(__name__)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def publish(cfg: Config, message: str) -> str:
    repo = cfg.repo_root
    if _git(repo, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        return "git repo 아님 → 건너뜀"
    _git(repo, "add", "--", "data")
    if _git(repo, "diff", "--cached", "--quiet", "--", "data", check=False).returncode == 0:
        return "data/ 변경 없음"
    # data/ 만 커밋한다(다른 스테이징 변경은 건드리지 않음)
    _git(repo, "commit", "-m", message, "--", "data")
    remotes = _git(repo, "remote").stdout.split()
    if cfg.publish_remote not in remotes:
        return f"커밋함, remote '{cfg.publish_remote}' 없음 → push 건너뜀"
    proc = _git(repo, "push", cfg.publish_remote, f"HEAD:{cfg.publish_branch}", check=False)
    if proc.returncode != 0:
        log.error("git push 실패: %s", proc.stderr.strip())
        return "커밋함, push 실패(로그 확인)"
    return f"커밋·push 완료 → {cfg.publish_remote}/{cfg.publish_branch}"
