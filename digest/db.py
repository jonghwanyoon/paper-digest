"""로컬 상태 DB(SQLite). 공개되지 않는다.

status 흐름:
  skipped        연구 논문이 아님(중복 판단용으로만 기록)
  new            수집됨, 근거 텍스트 확보 전
  awaiting_text  초록/전문을 아직 못 구함(매일 재시도)
  needs_pdf      재시도 기간이 지남 → PDF 투입 대기
  ready          근거 텍스트 있음, 요약 대기
  summarized     요약 완료
  failed         요약이 max_attempts 번 실패

개요 그림(요약 완료 논문만):
  ga_checked/ga_url   출판사 graphical abstract 확인 여부와 이미지 URL(repo 에 복사하지 않음)
  figure_path         AI 개요도 WebP 의 repo 상대 경로(data/figures/...)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  doi              TEXT PRIMARY KEY,
  journal          TEXT NOT NULL,
  title            TEXT NOT NULL,
  authors          TEXT NOT NULL DEFAULT '',
  url              TEXT NOT NULL DEFAULT '',
  article_type     TEXT,
  published        TEXT,
  teaser           TEXT,
  first_seen       TEXT NOT NULL,
  last_seen        TEXT NOT NULL,
  digest_date      TEXT NOT NULL,
  status           TEXT NOT NULL,
  pmid             TEXT,
  pmcid            TEXT,
  is_oa            INTEGER,
  oa_license       TEXT,
  source_level     TEXT,
  source_origin    TEXT,
  source_path      TEXT,
  enrich_attempts  INTEGER NOT NULL DEFAULT 0,
  last_enrich_at   TEXT,
  summary_json     TEXT,
  summary_level    TEXT,
  summary_engine   TEXT,
  summary_model    TEXT,
  summarized_at    TEXT,
  summary_attempts INTEGER NOT NULL DEFAULT 0,
  last_error       TEXT,
  relevance        REAL,
  flags_json       TEXT,
  ga_checked       INTEGER NOT NULL DEFAULT 0,
  ga_url           TEXT,
  figure_path      TEXT,
  figure_model     TEXT,
  figure_at        TEXT,
  figure_attempts  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS papers_status ON papers(status);
CREATE INDEX IF NOT EXISTS papers_digest_date ON papers(digest_date);
"""

# 이전 버전 DB 에 없던 열: (이름, 정의)
_ADDED_COLUMNS = [
    ("ga_checked", "INTEGER NOT NULL DEFAULT 0"),
    ("ga_url", "TEXT"),
    ("figure_path", "TEXT"),
    ("figure_model", "TEXT"),
    ("figure_at", "TEXT"),
    ("figure_attempts", "INTEGER NOT NULL DEFAULT 0"),
]

STATUSES = ("skipped", "new", "awaiting_text", "needs_pdf", "ready", "summarized", "failed")


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
    for name, decl in _ADDED_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {decl}")
    conn.commit()


def get(conn: sqlite3.Connection, doi: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()


def update(conn: sqlite3.Connection, doi: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE papers SET {cols} WHERE doi = ?", (*fields.values(), doi))
    conn.commit()


def by_status(conn: sqlite3.Connection, *statuses: str) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        f"SELECT * FROM papers WHERE status IN ({marks}) ORDER BY published DESC, doi",
        statuses,
    ).fetchall()


def status_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT journal, status, COALESCE(source_level, '-') AS source_level, COUNT(*) AS n "
        "FROM papers GROUP BY journal, status, source_level ORDER BY journal, status, source_level"
    ).fetchall()
