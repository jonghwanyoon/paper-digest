"""사용자가 준 PDF 로 근거를 전문 수준으로 올리고 재요약 대기열에 넣는다.

inbox/ 에 PDF 를 넣으면:
  1) 파일명이 DOI 슬러그(10.1016_j.cell.2026.09.017.pdf)면 그 논문,
  2) 아니면 앞 2쪽 텍스트에서 DB 에 있는 DOI 를 찾는다.
  매칭되면 raw/<slug>/paper.pdf 로 옮기고, 못 찾으면 inbox/unmatched/ 로 옮긴다.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path

from digest import db
from digest.config import Config
from digest.enrich import doi_slug
from digest.feeds import _DOI_RE, normalize_doi

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = 2000  # 이보다 짧으면 스캔 이미지 PDF 로 보고 거부


class PdfError(Exception):
    pass


def pdf_text(path: Path, last_page: int | None = None) -> str:
    cmd = ["pdftotext", "-enc", "UTF-8"]
    if last_page:
        cmd += ["-l", str(last_page)]
    try:
        proc = subprocess.run([*cmd, str(path), "-"], capture_output=True, text=True, timeout=120)
    except FileNotFoundError as e:
        raise PdfError("pdftotext 가 없음: sudo apt install poppler-utils") from e
    if proc.returncode != 0:
        raise PdfError(f"pdftotext 실패: {proc.stderr.strip()[:300]}")
    return proc.stdout


def find_doi(conn: sqlite3.Connection, path: Path) -> str | None:
    slugs = {doi_slug(r["doi"]): r["doi"] for r in conn.execute("SELECT doi FROM papers")}
    if path.stem.lower() in slugs:
        return slugs[path.stem.lower()]
    known = set(slugs.values())
    for m in _DOI_RE.finditer(pdf_text(path, last_page=2)):
        doi = normalize_doi(m.group(0))
        if doi in known:
            return doi
    return None


def attach_pdf(cfg: Config, conn: sqlite3.Connection, doi: str, pdf: Path, move: bool) -> Path:
    if not db.get(conn, doi):
        raise PdfError(f"DB 에 없는 DOI: {doi}")
    text = pdf_text(pdf)
    if len(text.strip()) < MIN_TEXT_CHARS:
        raise PdfError(f"추출 텍스트가 {len(text.strip())}자뿐(스캔 PDF?): {pdf}")
    d = cfg.raw_dir / doi_slug(doi)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / "paper.pdf"
    (shutil.move if move else shutil.copy2)(str(pdf), dest)
    txt = d / "pdf.txt"
    txt.write_text(text, encoding="utf-8")
    db.update(conn, doi, source_level="pdf", source_origin="pdf", source_path=str(txt),
              status="ready", summary_attempts=0, last_error=None)
    log.info("PDF 연결: %s → %s (재요약 대기)", pdf.name, doi)
    return dest


def ingest_inbox(cfg: Config, conn: sqlite3.Connection) -> list[tuple[str, str | None]]:
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for pdf in sorted(p for p in cfg.inbox_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"):
        try:
            doi = find_doi(conn, pdf)
            if doi:
                attach_pdf(cfg, conn, doi, pdf, move=True)
            else:
                unmatched = cfg.inbox_dir / "unmatched"
                unmatched.mkdir(exist_ok=True)
                shutil.move(str(pdf), unmatched / pdf.name)
                log.warning("DOI 를 찾지 못해 unmatched/ 로 옮김: %s (파일명을 DOI 슬러그로 바꿔 다시 넣으세요)", pdf.name)
            results.append((pdf.name, doi))
        except PdfError as e:
            log.error("PDF 처리 실패 → failed/ 로 옮김 %s: %s", pdf.name, e)
            if pdf.exists():
                failed = cfg.inbox_dir / "failed"
                failed.mkdir(exist_ok=True)
                shutil.move(str(pdf), failed / pdf.name)
            results.append((pdf.name, None))
    return results
