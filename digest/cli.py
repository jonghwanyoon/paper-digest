"""명령행: python3 -m digest <command>"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import logging
import sys
import textwrap
from datetime import datetime
from pathlib import Path

from digest import config, db, export, feeds, pdfs, publish
from digest.engines import get_engine
from digest.enrich import enrich_pending
from digest.feeds import normalize_doi
from digest.net import Client
from digest.summarize import rescore, summarize_pending

log = logging.getLogger("digest")


def setup_logging(cfg: config.Config, verbose: bool) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(cfg.log_dir / f"digest-{datetime.now(cfg.tz):%Y-%m-%d}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers[:] = [sh, fh]


def _engine_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--engine", choices=["codex", "claude"], help="settings.toml 의 summarizer.engine 덮어쓰기")
    p.add_argument("--model", help="모델 이름 덮어쓰기 (예: gpt-6.1-sol, opus)")
    p.add_argument("--effort", help="추론 강도 덮어쓰기 (예: xhigh, high)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python3 -m digest", description="저널 논문 데일리 다이제스트")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="데이터 디렉터리·DB 생성")
    sub.add_parser("status", help="저널·상태·근거별 집계")

    p = sub.add_parser("fetch", help="RSS 수집")
    p.add_argument("--journal", action="append", help="특정 저널만 (반복 가능)")

    p = sub.add_parser("enrich", help="초록·전문 확보")
    p.add_argument("--limit", type=int)

    p = sub.add_parser("summarize", help="요약 대기(ready) 논문 요약")
    p.add_argument("--limit", type=int)
    p.add_argument("--doi", action="append", help="특정 DOI 만")
    _engine_args(p)

    p = sub.add_parser("rescore", help="interests.md 변경 후 관련도만 재채점")
    _engine_args(p)

    p = sub.add_parser("resummarize", help="지정 논문을 다시 요약 대기로")
    p.add_argument("doi", nargs="+")

    sub.add_parser("ingest-pdf", help="inbox/ 의 PDF 처리")
    p = sub.add_parser("add-pdf", help="PDF 를 특정 DOI 에 연결(파일은 복사)")
    p.add_argument("doi")
    p.add_argument("pdf", type=Path)

    sub.add_parser("export", help="공개 JSON(data/) 쓰기")
    p = sub.add_parser("build", help="data/ → _site/ 정적 사이트")
    p.add_argument("--out", type=Path)
    sub.add_parser("publish", help="data/ 커밋·push")

    p = sub.add_parser("run", help="fetch → ingest-pdf → enrich → summarize → export → publish")
    p.add_argument("--limit", type=int, help="이번 실행에서 요약할 최대 편수")
    p.add_argument("--no-summarize", action="store_true")
    p.add_argument("--no-publish", action="store_true")
    _engine_args(p)

    p = sub.add_parser("list", help="논문 목록")
    p.add_argument("--status")
    p.add_argument("--date", help="digest 날짜 YYYY-MM-DD")
    p.add_argument("--journal")
    p.add_argument("--all", action="store_true", help="비연구(skipped) 포함")
    p.add_argument("--limit", type=int, default=200)

    p = sub.add_parser("show", help="논문 한 편 상세(로컬 정보 포함)")
    p.add_argument("doi")
    return ap


def _client(cfg):
    return Client(cfg.email)


def cmd_status(cfg, conn, _args) -> int:
    rows = db.status_counts(conn)
    if not rows:
        print("DB 비어 있음. 먼저 fetch 하세요.")
        return 0
    print(f"{'journal':<10} {'status':<14} {'source':<10} {'n':>5}")
    for r in rows:
        print(f"{r['journal']:<10} {r['status']:<14} {r['source_level']:<10} {r['n']:>5}")
    pending = len(db.by_status(conn, "ready"))
    print(f"\n요약 대기: {pending}편 · DB: {cfg.db_path}")
    return 0


def cmd_fetch(cfg, conn, args) -> int:
    res = feeds.fetch_all(cfg, conn, _client(cfg), args.journal)
    for key, (n, new_r, new_o) in res.items():
        print(f"{key:<10} 피드 항목 {n:>4} · 신규 연구 {new_r:>3} · 신규 기타 {new_o:>3}")
    return 0


def cmd_enrich(cfg, conn, args) -> int:
    counts = enrich_pending(cfg, conn, _client(cfg), args.limit)
    print("enrich 결과:", counts or "대상 없음")
    return 0


def _engine(cfg, args):
    es = cfg.engine_settings(args.engine, args.model, args.effort)
    return es, get_engine(es)


def _report(res, label: str) -> None:
    msg = f"{label}: 완료 {res.done} · 실패 {res.failed}"
    if res.stopped == "quota":
        msg += f" · 사용량 한도로 중단(남은 {res.remaining}편은 다음 실행으로)"
    elif res.stopped == "unavailable":
        msg += f" · 엔진 사용 불가로 중단(남은 {res.remaining}편)"
    print(msg)


def cmd_summarize(cfg, conn, args) -> int:
    es, engine = _engine(cfg, args)
    print(f"엔진: {es.engine} · 모델: {es.model or '(기본)'} · effort: {es.effort or '(기본)'}")
    dois = [normalize_doi(d) for d in args.doi] if args.doi else None
    res = summarize_pending(cfg, conn, engine, es, args.limit, dois)
    _report(res, "요약")
    return 2 if res.stopped == "unavailable" else 0


def cmd_rescore(cfg, conn, args) -> int:
    _es, engine = _engine(cfg, args)
    res = rescore(cfg, conn, engine)
    _report(res, "재채점")
    return 2 if res.stopped == "unavailable" else 0


def cmd_resummarize(cfg, conn, args) -> int:
    for raw in args.doi:
        doi = normalize_doi(raw)
        row = db.get(conn, doi) if doi else None
        if not row or not row["source_path"]:
            print(f"근거 텍스트가 없는 DOI: {raw}")
            continue
        db.update(conn, doi, status="ready", summary_attempts=0)
        print(f"요약 대기로 전환: {doi}")
    return 0


def cmd_ingest_pdf(cfg, conn, _args) -> int:
    results = pdfs.ingest_inbox(cfg, conn)
    if not results:
        print(f"inbox 비어 있음: {cfg.inbox_dir}")
    for name, doi in results:
        print(f"{name} → {doi or '실패 (inbox/unmatched/ 또는 inbox/failed/ 와 로그 확인)'}")
    return 0


def cmd_add_pdf(cfg, conn, args) -> int:
    doi = normalize_doi(args.doi)
    try:
        dest = pdfs.attach_pdf(cfg, conn, doi or args.doi, args.pdf, move=False)
    except pdfs.PdfError as e:
        print(f"실패: {e}")
        return 1
    print(f"{doi} ← {args.pdf} (저장: {dest}) · 다음 summarize/run 에서 PDF 근거로 재요약")
    return 0


def cmd_export(cfg, conn, _args) -> int:
    changed = export.export(cfg, conn)
    print(f"변경된 파일 {len(changed)}개" + "".join(f"\n  {p.relative_to(cfg.repo_root)}" for p in changed))
    return 0


def _site_builder(cfg):
    spec = importlib.util.spec_from_file_location("site_build", cfg.repo_root / "site" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_build(cfg, _conn, args) -> int:
    out = args.out or cfg.repo_root / "_site"
    written = _site_builder(cfg).build(cfg.public_data_dir, out)
    print(f"{len(written)} pages → {out}")
    return 0


def cmd_publish(cfg, _conn, _args) -> int:
    print(publish.publish(cfg, f"digest: {datetime.now(cfg.tz):%Y-%m-%d}"))
    return 0


def cmd_run(cfg, conn, args) -> int:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    lock = open(cfg.data_dir / "run.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("다른 run 이 실행 중이라 종료합니다.")
        return 0
    client = _client(cfg)
    log.info("=== run 시작")
    fetched = feeds.fetch_all(cfg, conn, client)
    log.info("fetch: %s", {k: f"신규 연구 {v[1]}" for k, v in fetched.items()})
    for name, doi in pdfs.ingest_inbox(cfg, conn):
        log.info("PDF %s → %s", name, doi or "매칭 실패")
    log.info("enrich: %s", enrich_pending(cfg, conn, client) or "대상 없음")
    code = 0
    if not args.no_summarize:
        es, engine = _engine(cfg, args)
        res = summarize_pending(cfg, conn, engine, es, args.limit)
        log.info("summarize: 완료 %d · 실패 %d · 중단 %s · 남음 %d",
                 res.done, res.failed, res.stopped or "-", res.remaining)
        if res.stopped == "unavailable":
            code = 2
    changed = export.export(cfg, conn)
    log.info("export: 변경 %d개", len(changed))
    if cfg.publish_enabled and not args.no_publish:
        log.info("publish: %s", publish.publish(cfg, f"digest: {datetime.now(cfg.tz):%Y-%m-%d}"))
    log.info("=== run 끝 (exit %d)", code)
    return code


def cmd_list(cfg, conn, args) -> int:
    q, params = "SELECT * FROM papers WHERE 1=1", []
    if not args.all:
        q += " AND status != 'skipped'"
    for col, val in (("status", args.status), ("digest_date", args.date), ("journal", args.journal)):
        if val:
            q += f" AND {col} = ?"
            params.append(val)
    q += " ORDER BY digest_date DESC, relevance DESC, journal, doi LIMIT ?"
    params.append(args.limit)
    rows = conn.execute(q, params).fetchall()
    for r in rows:
        rel = "-" if r["relevance"] is None else int(r["relevance"])
        print(f"{r['digest_date']} {r['journal']:<8} {r['status']:<13} {(r['source_level'] or '-'):<8} "
              f"{rel:>2} {r['doi']:<32} {r['title'][:70]}")
    print(f"({len(rows)}편)")
    return 0


def cmd_show(cfg, conn, args) -> int:
    row = db.get(conn, normalize_doi(args.doi) or args.doi)
    if not row:
        print("없음")
        return 1
    for k in row.keys():
        if k in ("summary_json", "teaser"):
            continue
        if row[k] is not None:
            print(f"{k:<17} {row[k]}")
    if row["summary_json"]:
        s = json.loads(row["summary_json"])
        print("\n--- 요약")
        print(f"[{s['title_ko']}]\n{textwrap.fill(s['tldr'], 100)}")
        for f in s["key_findings"]:
            print(textwrap.fill(f, 100, initial_indent="  - ", subsequent_indent="    "))
        for k in ("methods", "limitations", "relevance_reason"):
            if s.get(k):
                print(textwrap.fill(s[k], 100, initial_indent=f"{k}: ", subsequent_indent="    "))
        print("keywords:", ", ".join(s["keywords"]))
    return 0


COMMANDS = {
    "init": lambda cfg, conn, a: print(f"OK: {cfg.data_dir}\n  DB: {cfg.db_path}\n  inbox: {cfg.inbox_dir}") or 0,
    "status": cmd_status, "fetch": cmd_fetch, "enrich": cmd_enrich, "summarize": cmd_summarize,
    "rescore": cmd_rescore, "resummarize": cmd_resummarize, "ingest-pdf": cmd_ingest_pdf,
    "add-pdf": cmd_add_pdf, "export": cmd_export, "build": cmd_build, "publish": cmd_publish,
    "run": cmd_run, "list": cmd_list, "show": cmd_show,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config.load()
    setup_logging(cfg, args.verbose)
    for d in (cfg.data_dir, cfg.raw_dir, cfg.inbox_dir):
        d.mkdir(parents=True, exist_ok=True)
    conn = db.connect(cfg.db_path)
    try:
        return COMMANDS[args.cmd](cfg, conn, args)
    finally:
        conn.close()
