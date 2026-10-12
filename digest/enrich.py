"""DOI 로 근거 텍스트(전문 > 초록)를 확보해 로컬 raw/ 에 저장한다.

전문:  Europe PMC fullTextXML(PMCID 있을 때) → nature.com OA 논문 HTML
초록:  PubMed → Crossref(JATS) → nature.com 페이지 초록 → OpenAlex(inverted index)
science.org / cell.com 페이지는 Cloudflare 403 이라 요청하지 않는다.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from digest import db
from digest.config import Config
from digest.net import Client, HttpError
from digest.textclean import (BACK_MATTER_SECTIONS, drop_sections, jats_abstract, markup_to_text,
                              nature_article, openalex_abstract)

log = logging.getLogger(__name__)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"


def doi_slug(doi: str) -> str:
    """DOI → 파일/디렉터리 이름. PDF 투입 파일명 규칙과 같다(10.1016_j.cell.2026.09.017)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", doi.lower())


@dataclass
class Evidence:
    pmid: str | None = None
    pmcid: str | None = None
    is_oa: bool | None = None
    license: str | None = None
    fulltext: tuple[str, str] | None = None  # (origin, text)
    abstract: tuple[str, str] | None = None
    notes: list[str] = field(default_factory=list)


class Sources:
    def __init__(self, cfg: Config, client: Client):
        self.cfg = cfg
        self.client = client

    def _json(self, url: str) -> dict | None:
        try:
            return json.loads(self.client.get(url, accept="application/json"))
        except HttpError as e:
            if e.status == 404:
                return None
            raise

    # --- OpenAlex: OA 여부, 라이선스, PMID/PMCID, 초록(inverted index)
    def openalex(self, doi: str) -> dict | None:
        params = {"mailto": self.cfg.email}
        if self.cfg.openalex_key:
            params["api_key"] = self.cfg.openalex_key
        return self._json(f"https://api.openalex.org/works/doi:{quote(doi)}?{urlencode(params)}")

    # --- Crossref JATS 초록
    def crossref_abstract(self, doi: str) -> str | None:
        data = self._json(f"https://api.crossref.org/works/{quote(doi)}?mailto={quote(self.cfg.email)}")
        raw = (data or {}).get("message", {}).get("abstract")
        return jats_abstract(raw) if raw else None

    # --- PubMed
    def _eutils(self, endpoint: str, **params) -> bytes:
        params.update(tool="paper-digest", email=self.cfg.email)
        return self.client.get(f"{EUTILS}{endpoint}?{urlencode(params)}")

    def pubmed_id(self, doi: str) -> str | None:
        data = json.loads(self._eutils("esearch.fcgi", db="pubmed", retmode="json", term=f'"{doi}"[doi]'))
        ids = data.get("esearchresult", {}).get("idlist", [])
        return ids[0] if len(ids) == 1 else None

    def pubmed_record(self, pmid: str) -> tuple[str | None, str | None]:
        """(초록, PMCID)"""
        root = ET.fromstring(self._eutils("efetch.fcgi", db="pubmed", retmode="xml", id=pmid))
        art = root.find("PubmedArticle")
        if art is None:
            return None, None
        parts = []
        for t in art.findall("MedlineCitation/Article/Abstract/AbstractText"):
            text = re.sub(r"\s+", " ", "".join(t.itertext())).strip()
            if text:
                label = t.get("Label")
                parts.append(f"{label}: {text}" if label else text)
        pmcid = None
        for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):  # 직계만(참고문헌 ID 제외)
            if aid.get("IdType") == "pmc" and aid.text:
                pmcid = aid.text.strip()
        return ("\n\n".join(parts) or None), pmcid

    # --- Europe PMC 전문 (OA subset 만 fullTextXML 제공, 나머지는 HTTP 500 이라 먼저 확인)
    def europepmc_fulltext(self, pmcid: str) -> str | None:
        meta = self._json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
                          + urlencode({"query": f"PMCID:{pmcid}", "format": "json", "resultType": "lite"}))
        hits = (meta or {}).get("resultList", {}).get("result", [])
        if not any(h.get("pmcid") == pmcid and h.get("isOpenAccess") == "Y" for h in hits):
            return None
        try:
            xml = self.client.get(
                f"https://www.ebi.ac.uk/europepmc/webservices/rest/{quote(pmcid)}/fullTextXML")
        except HttpError as e:
            if e.status == 404:
                return None
            raise
        text = xml.decode("utf-8", "replace")
        abstract = re.search(r"<abstract[^>]*>(.*?)</abstract>", text, re.S)
        body = re.search(r"<body[^>]*>(.*?)</body>", text, re.S)  # 첫 body 만(부속 문서 제외)
        if not body:
            return None
        out = ""
        if abstract:
            out += "## Abstract\n" + jats_abstract(abstract.group(1)) + "\n\n"
        out = drop_sections(out + markup_to_text(body.group(1)), BACK_MATTER_SECTIONS)
        return out if len(out) > 3000 else None

    # --- nature.com 논문 페이지
    def nature_page(self, doi: str) -> tuple[str | None, str | None]:
        suffix = doi.split("/", 1)[1]
        try:
            page = self.client.get(f"https://www.nature.com/articles/{quote(suffix)}")
        except HttpError as e:
            if e.status == 404:
                return None, None
            raise
        return nature_article(page.decode("utf-8", "replace"))


def gather(src: Sources, doi: str, html_fulltext: str | None, known_pmid: str | None = None) -> Evidence:
    ev = Evidence(pmid=known_pmid)
    oa = None
    try:
        oa = src.openalex(doi)
    except HttpError as e:
        ev.notes.append(f"openalex: {e}")
    if oa:
        ids = oa.get("ids") or {}
        if ids.get("pmid"):
            ev.pmid = ids["pmid"].rstrip("/").rsplit("/", 1)[-1]
        if ids.get("pmcid"):
            ev.pmcid = ids["pmcid"].rstrip("/").rsplit("/", 1)[-1]
        ev.is_oa = bool((oa.get("open_access") or {}).get("is_oa"))
        ev.license = (oa.get("best_oa_location") or {}).get("license")

    # PubMed: PMID 확보 + 초록 + PMCID
    pubmed_abs = None
    try:
        if not ev.pmid:
            ev.pmid = src.pubmed_id(doi)
        if ev.pmid:
            pubmed_abs, pmcid = src.pubmed_record(ev.pmid)
            ev.pmcid = ev.pmcid or pmcid
    except (HttpError, ET.ParseError, json.JSONDecodeError) as e:
        ev.notes.append(f"pubmed: {e}")

    if ev.pmcid:
        try:
            full = src.europepmc_fulltext(ev.pmcid)
            if full:
                ev.fulltext = ("europepmc", full)
        except HttpError as e:
            ev.notes.append(f"europepmc: {e}")

    nature_abs = None
    if html_fulltext == "nature":
        try:
            nature_abs, full = src.nature_page(doi)
            if full and not ev.fulltext:
                ev.fulltext = ("nature-html", full)
        except HttpError as e:
            ev.notes.append(f"nature: {e}")

    if pubmed_abs:
        ev.abstract = ("pubmed", pubmed_abs)
    else:
        try:
            cr = src.crossref_abstract(doi)
            if cr:
                ev.abstract = ("crossref", cr)
        except HttpError as e:
            ev.notes.append(f"crossref: {e}")
    if not ev.abstract and nature_abs:
        ev.abstract = ("nature-html", nature_abs)
    if not ev.abstract and oa:
        inv = openalex_abstract(oa.get("abstract_inverted_index"))
        if inv:
            ev.abstract = ("openalex", inv)
    return ev


def _save(cfg: Config, doi: str, name: str, text: str) -> Path:
    d = cfg.raw_dir / doi_slug(doi)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def enrich_row(cfg: Config, conn: sqlite3.Connection, src: Sources, row: sqlite3.Row) -> str:
    journal = cfg.journals.get(row["journal"])
    ev = gather(src, row["doi"], journal.html_fulltext if journal else None, row["pmid"])
    if ev.abstract:
        _save(cfg, row["doi"], "abstract.txt", ev.abstract[1])
    fields = dict(
        pmid=ev.pmid, pmcid=ev.pmcid,
        is_oa=None if ev.is_oa is None else int(ev.is_oa), oa_license=ev.license,
        enrich_attempts=row["enrich_attempts"] + 1, last_enrich_at=db.utcnow(),
        last_error="; ".join(ev.notes) or None,
    )
    if ev.fulltext:
        origin, text = ev.fulltext
        fields.update(source_level="fulltext", source_origin=origin,
                      source_path=str(_save(cfg, row["doi"], "fulltext.txt", text)), status="ready")
    elif ev.abstract:
        fields.update(source_level="abstract", source_origin=ev.abstract[0],
                      source_path=str(cfg.raw_dir / doi_slug(row["doi"]) / "abstract.txt"),
                      status="ready")
    else:
        first_seen = datetime.fromisoformat(row["first_seen"])
        expired = datetime.now(timezone.utc) - first_seen > timedelta(days=cfg.abstract_wait_days)
        fields.update(status="needs_pdf" if expired else "awaiting_text")
    db.update(conn, row["doi"], **fields)
    return fields["status"] if fields["status"] != "ready" else fields["source_level"]


def enrich_pending(cfg: Config, conn: sqlite3.Connection, client: Client, limit: int | None = None) -> dict[str, int]:
    src = Sources(cfg, client)
    rows = db.by_status(conn, "new", "awaiting_text")
    if limit:
        rows = rows[:limit]
    counts: dict[str, int] = {}
    for row in rows:
        outcome = enrich_row(cfg, conn, src, row)
        counts[outcome] = counts.get(outcome, 0) + 1
        log.info("[%s] %s → %s", row["journal"], row["doi"], outcome)
    return counts
