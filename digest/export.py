"""공개용 JSON 내보내기: data/papers/YYYY-MM.json, data/meta.json

허용 목록 방식으로 필드를 고른다. 초록·전문·RSS 소개문 같은 원문 텍스트는 넣지 않는다.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from digest.config import Config

MAX_AUTHORS = 8


def _authors(raw: str) -> str:
    names = [a.strip() for a in raw.split(",") if a.strip()]
    if len(names) <= MAX_AUTHORS:
        return ", ".join(names)
    return ", ".join(names[:MAX_AUTHORS]) + f" 외 {len(names) - MAX_AUTHORS}명"


def public_record(cfg: Config, r: sqlite3.Row) -> dict:
    summary = json.loads(r["summary_json"]) if r["summary_json"] else None
    flags = json.loads(r["flags_json"]) if r["flags_json"] else {}
    journal = cfg.journals.get(r["journal"])
    return {
        "doi": r["doi"],
        "journal": r["journal"],
        "journal_name": journal.name if journal else r["journal"],
        "title": r["title"],
        "authors": _authors(r["authors"]),
        "url": f"https://doi.org/{r['doi']}",
        "article_type": r["article_type"],
        "published": r["published"],
        "digest_date": r["digest_date"],
        "status": r["status"],
        "is_oa": None if r["is_oa"] is None else bool(r["is_oa"]),
        "license": r["oa_license"],
        # 요약이 있으면 요약에 쓴 근거, 없으면 현재 확보한 근거
        "source_level": r["summary_level"] if summary else r["source_level"],
        "pdf_pending": r["source_level"] == "pdf" and r["status"] == "ready",
        "summary": summary,
        "summarized_at": r["summarized_at"],
        "relevance": None if r["relevance"] is None else int(r["relevance"]),
        "unverified_numbers": flags.get("unverified_numbers", []),
        # 출판사 graphical abstract: 이미지 URL 만(사이트가 출판사 서버에서 직접 표시)
        "graphical_abstract": r["ga_url"],
        # AI 개요도: data/ 기준 상대 경로(WebP)
        "figure": r["figure_path"],
    }


def _write_if_changed(path: Path, data) -> bool:
    text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def export(cfg: Config, conn: sqlite3.Connection) -> list[Path]:
    rows = conn.execute(
        "SELECT * FROM papers WHERE status != 'skipped' ORDER BY digest_date, journal, doi").fetchall()
    by_month: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_month[r["digest_date"][:7]].append(public_record(cfg, r))
    out = cfg.public_data_dir
    changed = []
    meta = {"site_title": cfg.site_title,
            "journals": {k: j.name for k, j in cfg.journals.items()}}
    if _write_if_changed(out / "meta.json", meta):
        changed.append(out / "meta.json")
    for month, records in by_month.items():
        p = out / "papers" / f"{month}.json"
        if _write_if_changed(p, records):
            changed.append(p)
    return changed
