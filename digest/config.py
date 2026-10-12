"""설정 파일(config/*.toml, interests.md) 로드."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Journal:
    key: str
    name: str
    feeds: list[str]
    doi_prefixes: list[str] = field(default_factory=list)
    include_types: list[str] = field(default_factory=list)
    exclude_title: list[re.Pattern] = field(default_factory=list)
    html_fulltext: str | None = None

    def is_research(self, doi: str, article_type: str | None, title: str) -> bool:
        if self.doi_prefixes and not any(doi.startswith(p) for p in self.doi_prefixes):
            return False
        if self.include_types and (article_type or "") not in self.include_types:
            return False
        return not any(p.search(title) for p in self.exclude_title)


@dataclass(frozen=True)
class EngineSettings:
    engine: str
    model: str
    effort: str
    timeout_sec: int
    max_attempts: int
    max_input_chars: int


@dataclass(frozen=True)
class Config:
    repo_root: Path
    data_dir: Path
    email: str
    openalex_key: str
    abstract_wait_days: int
    summarizer: dict
    site_title: str
    tz: ZoneInfo
    publish_enabled: bool
    publish_remote: str
    publish_branch: str
    journals: dict[str, Journal]
    interests_path: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def public_data_dir(self) -> Path:
        return self.repo_root / "data"

    def interests(self) -> str:
        return self.interests_path.read_text(encoding="utf-8")

    def engine_settings(self, engine: str | None = None, model: str | None = None,
                        effort: str | None = None) -> EngineSettings:
        s = self.summarizer
        name = engine or s.get("engine", "codex")
        per = s.get(name, {})
        return EngineSettings(
            engine=name,
            model=model or per.get("model", ""),
            effort=effort or per.get("effort", ""),
            timeout_sec=int(s.get("timeout_sec", 1800)),
            max_attempts=int(s.get("max_attempts", 3)),
            max_input_chars=int(s.get("max_input_chars", 150000)),
        )


def load(repo_root: Path = REPO_ROOT) -> Config:
    cfg_dir = repo_root / "config"
    with open(cfg_dir / "settings.toml", "rb") as f:
        s = tomllib.load(f)
    with open(cfg_dir / "journals.toml", "rb") as f:
        j = tomllib.load(f)

    journals = {}
    for item in j.get("journal", []):
        journals[item["key"]] = Journal(
            key=item["key"],
            name=item.get("name", item["key"]),
            feeds=list(item["feeds"]),
            doi_prefixes=[p.lower() for p in item.get("doi_prefixes", [])],
            include_types=list(item.get("include_types", [])),
            exclude_title=[re.compile(p, re.I) for p in item.get("exclude_title", [])],
            html_fulltext=item.get("html_fulltext"),
        )

    data_dir = os.environ.get("PAPER_DIGEST_HOME") or s["paths"]["data_dir"]
    publish = s.get("publish", {})
    return Config(
        repo_root=repo_root,
        data_dir=Path(data_dir).expanduser(),
        email=s["contact"]["email"],
        openalex_key=s.get("openalex", {}).get("api_key", ""),
        abstract_wait_days=int(s.get("enrich", {}).get("abstract_wait_days", 7)),
        summarizer=s.get("summarizer", {}),
        site_title=s.get("site", {}).get("title", "Journal Daily Digest"),
        tz=ZoneInfo(s.get("site", {}).get("timezone", "Asia/Seoul")),
        publish_enabled=bool(publish.get("enabled", True)),
        publish_remote=publish.get("remote", "origin"),
        publish_branch=publish.get("branch", "main"),
        journals=journals,
        interests_path=cfg_dir / "interests.md",
    )
