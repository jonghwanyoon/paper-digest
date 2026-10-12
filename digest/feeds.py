"""RSS 1.0/2.0 · Atom 피드 수집, 연구 논문 판별, DOI 기준 중복 제거."""

from __future__ import annotations

import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from digest import db
from digest.config import Config, Journal
from digest.net import Client, HttpError
from digest.textclean import inline_text

log = logging.getLogger(__name__)

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>?#]+", re.I)


@dataclass
class FeedItem:
    doi: str
    title: str
    url: str
    authors: str
    article_type: str | None
    published: str | None
    teaser: str | None


def normalize_doi(raw: str | None) -> str | None:
    if not raw:
        return None
    m = _DOI_RE.search(raw)
    return m.group(0).rstrip(".").lower() if m else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(payload: bytes) -> list[FeedItem]:
    root = ET.fromstring(payload)
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        fields: dict[str, list[str]] = {}
        link_href = None
        for child in el:
            name = _local(child.tag)
            if name == "link" and child.get("href"):
                link_href = child.get("href")
            fields.setdefault(name, []).append((child.text or "").strip())

        def first(*names):
            for n in names:
                for v in fields.get(n, []):
                    if v:
                        return v
            return None

        link = first("link") or link_href or ""
        doi = normalize_doi(first("doi")) or normalize_doi(first("identifier")) or normalize_doi(link)
        if not doi:
            log.debug("DOI 없는 항목 건너뜀: %s", first("title"))
            continue
        date = first("publicationDate", "date", "pubDate", "published", "updated")
        items.append(FeedItem(
            doi=doi,
            title=inline_text(first("title") or ""),
            url=re.sub(r"\?(af=R|rss=yes)$", "", link),
            authors=", ".join(inline_text(v) for v in fields.get("creator", []) if v),
            article_type=first("type", "section"),
            published=_iso_date(date),
            teaser=inline_text(first("description", "encoded", "summary") or "") or None,
        ))
    return items


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    m = re.match(r"\d{4}-\d{2}-\d{2}", value)
    if m:
        return m.group(0)
    try:  # RSS 2.0 pubDate (RFC 822)
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        return None


def store(conn: sqlite3.Connection, journal: Journal, items: list[FeedItem], today: str) -> tuple[int, int]:
    """신규 연구 논문 수, 신규 비연구 항목 수를 돌려준다. 이미 본 DOI 는 last_seen 만 갱신."""
    now = db.utcnow()
    new_research = new_other = 0
    for it in items:
        if db.get(conn, it.doi):
            conn.execute("UPDATE papers SET last_seen = ? WHERE doi = ?", (now, it.doi))
            continue
        research = journal.is_research(it.doi, it.article_type, it.title)
        conn.execute(
            "INSERT INTO papers (doi, journal, title, authors, url, article_type, published, teaser,"
            " first_seen, last_seen, digest_date, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (it.doi, journal.key, it.title, it.authors, it.url, it.article_type, it.published,
             it.teaser, now, now, today, "new" if research else "skipped"),
        )
        if research:
            new_research += 1
        else:
            new_other += 1
    conn.commit()
    return new_research, new_other


def fetch_all(cfg: Config, conn: sqlite3.Connection, client: Client,
              journal_keys: list[str] | None = None) -> dict[str, tuple[int, int, int]]:
    """저널별 (피드 항목 수, 신규 연구, 신규 기타)."""
    today = datetime.now(cfg.tz).date().isoformat()
    result = {}
    for key, journal in cfg.journals.items():
        if journal_keys and key not in journal_keys:
            continue
        seen: dict[str, FeedItem] = {}
        for url in journal.feeds:
            try:
                for it in parse_feed(client.get(url)):
                    seen.setdefault(it.doi, it)
            except (HttpError, ET.ParseError) as e:
                log.error("[%s] 피드 실패 %s: %s", key, url, e)
        items = list(seen.values())
        new_r, new_o = store(conn, journal, items, today)
        excluded = Counter(it.article_type or it.doi.split("/", 1)[-1].split("-", 1)[0]
                           for it in items if not journal.is_research(it.doi, it.article_type, it.title))
        if excluded:
            log.info("[%s] 연구 논문 아님으로 판별된 유형: %s", key, dict(excluded))
        result[key] = (len(items), new_r, new_o)
    return result
