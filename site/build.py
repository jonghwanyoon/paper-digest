#!/usr/bin/env python3
"""data/*.json → 정적 HTML(_site/). 표준 라이브러리만 쓰며 GitHub Actions 에서도 그대로 돈다.

페이지 구성(블로그형):
  index.html               최근 날짜들의 요약 논문을 포토카드 그리드로
  YYYY-MM-DD.html          그날 들어온 논문 카드 + 요약 대기 목록
  paper-<doi 슬러그>.html  논문 한 편 상세(그림·요약·차별점·한계)
  archive.html             날짜 목록

모든 텍스트(특히 모델 출력)는 html.escape 를 거친다. JavaScript 는 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

INDEX_DAYS = 7  # 첫 화면에 보여줄 최근 날짜 수

SOURCE_LABEL = {"fulltext": "전문", "abstract": "초록", "pdf": "PDF"}
STATUS_NOTE = {
    "new": "근거 텍스트 확보 전",
    "awaiting_text": "초록·전문 확보 대기 중",
    "needs_pdf": "초록·전문을 구하지 못함 — PDF 필요",
    "ready": "요약 대기 중",
    "failed": "요약 실패",
}
LEVEL_NOTE = {"abstract": "초록만 보고 작성한 요약입니다. 세부 방법·수치는 원문에서 확인하세요."}

_FIGURE_PATH = re.compile(r"figures/[A-Za-z0-9._-]+\.webp")


def e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def slug(doi: str) -> str:
    """digest.enrich.doi_slug 와 같은 규칙(이 스크립트는 digest 패키지 없이 돈다)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", doi.lower())


def paper_href(p: dict) -> str:
    return f"paper-{slug(p['doi'])}.html"


def load(data_dir: Path) -> tuple[dict, list[dict]]:
    meta_path = data_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    papers = []
    for p in sorted((data_dir / "papers").glob("*.json")):
        papers.extend(json.loads(p.read_text(encoding="utf-8")))
    return meta, papers


def sort_key(p: dict):
    summarized = p.get("summary") is not None
    return (0 if summarized else 1, -(p.get("relevance") or 0), p.get("journal", ""), p.get("title", ""))


def figure_of(p: dict) -> tuple[str, str, str | None] | None:
    """(종류 'ga'|'ai', 표시 이미지 URL, 크게 보기 URL). 출판사 GA 가 우선."""
    ga = p.get("graphical_abstract")
    if ga and ga.startswith("https://"):
        return "ga", ga, (ga[:-4] + "_lrg.jpg" if ga.endswith("-ga1.jpg") else ga)
    fig = p.get("figure")
    if fig and _FIGURE_PATH.fullmatch(fig):
        return "ai", fig, fig
    return None


def rel_class(rel) -> str:
    rel = rel or 0
    return "hi" if rel >= 8 else "mid" if rel >= 5 else "low"


# ---------------------------------------------------------------- 카드

def render_card(p: dict) -> str:
    s = p["summary"]
    fig = figure_of(p)
    if fig:
        kind, src, _ = fig
        ref = ' referrerpolicy="no-referrer"' if kind == "ga" else ""
        tag = "Graphical abstract" if kind == "ga" else "AI 개요도"
        media = (f'<div class="card-media"><img src="{e(src)}" alt="" loading="lazy"{ref}>'
                 f'<span class="media-tag">{tag}</span>')
    else:
        words = " · ".join(s.get("keywords", [])[:3])
        media = (f'<div class="card-media cover cover-{e(p.get("journal"))}">'
                 f'<span class="cover-journal">{e(p.get("journal_name"))}</span>'
                 f'<span class="cover-words">{e(words)}</span>')
    media += f'<span class="rel rel-{rel_class(p.get("relevance"))}">관련도 {e(p.get("relevance"))}</span></div>'
    level = p.get("source_level")
    meta = [p.get("journal_name"), p.get("article_type"),
            f"근거: {SOURCE_LABEL.get(level, level)}" if level else None]
    return (f'<a class="card" href="{e(paper_href(p))}">{media}'
            f'<div class="card-body">'
            f'<p class="card-meta">{" · ".join(e(m) for m in meta if m)}</p>'
            f'<h3 class="card-title">{e(s.get("title_ko") or p.get("title"))}</h3>'
            f'<p class="card-en">{e(p.get("title"))}</p>'
            f'<p class="card-tldr">{e(s.get("tldr"))}</p>'
            f'</div></a>')


def card_grid(papers: list[dict]) -> str:
    return '<div class="grid">' + "".join(render_card(p) for p in papers) + "</div>"


def render_pending(p: dict) -> str:
    note = STATUS_NOTE.get(p.get("status"), p.get("status"))
    return (f'<li><span class="chip">{e(p.get("journal_name"))}</span> '
            f'<a href="{e(p.get("url"))}" rel="noopener" target="_blank">{e(p.get("title"))}</a> '
            f'<span class="note status-{e(p.get("status"))}">{e(note)}</span></li>')


def pending_block(pending: list[dict], open_: bool) -> str:
    if not pending:
        return ""
    return (f'<details class="pending"{" open" if open_ else ""}>'
            f'<summary>아직 요약 전인 논문 {len(pending)}편</summary><ul>'
            + "".join(render_pending(p) for p in pending) + "</ul></details>")


# ---------------------------------------------------------------- 상세

def render_article(p: dict) -> str:
    s = p["summary"]
    level = p.get("source_level")
    chips = [f'<span class="rel rel-{rel_class(p.get("relevance"))}">관련도 {e(p.get("relevance"))}</span>',
             f'<span class="chip">{e(p.get("journal_name"))}</span>']
    if p.get("article_type"):
        chips.append(f'<span class="chip">{e(p["article_type"])}</span>')
    if level:
        chips.append(f'<span class="chip src-{e(level)}">근거: {e(SOURCE_LABEL.get(level, level))}</span>')
    if p.get("is_oa"):
        chips.append('<span class="chip oa">Open Access</span>')
    if p.get("pdf_pending"):
        chips.append('<span class="chip src-pdf">PDF 재요약 대기</span>')

    parts = [f'<nav class="crumb"><a href="{e(p["digest_date"])}.html">← {e(p["digest_date"])} 논문</a></nav>',
             '<article class="post">',
             f'<div class="chips">{"".join(chips)}</div>',
             f'<h1>{e(s.get("title_ko") or p.get("title"))}</h1>',
             f'<p class="post-en">{e(p.get("title"))}</p>',
             f'<p class="post-authors">{e(p.get("authors"))}</p>']

    fig = figure_of(p)
    if fig:
        kind, src, large = fig
        ref = ' referrerpolicy="no-referrer"' if kind == "ga" else ""
        caption = (f"Graphical abstract · © {e(p.get('journal_name'))} (출판사 이미지, 누르면 크게 보기)"
                   if kind == "ga" else
                   "AI 생성 개요도 — 요약을 바탕으로 그린 그림이며 논문 원본 그림이 아닙니다. 세부 내용이 부정확할 수 있습니다.")
        parts.append(f'<figure class="post-figure {kind}"><a href="{e(large)}" target="_blank" rel="noopener">'
                     f'<img src="{e(src)}" alt="{"Graphical abstract" if kind == "ga" else "AI 생성 개요도"}"{ref}></a>'
                     f'<figcaption>{caption}</figcaption></figure>')

    parts.append(f'<p class="lead">{e(s.get("tldr"))}</p>')
    sections = [
        ("요약", s.get("summary")),
        ("기존 연구와의 차별점", s.get("novelty") or "근거 텍스트에 기존 연구와의 비교가 없습니다."),
        ("한계", s.get("limitations") or "근거 텍스트에 한계가 언급되지 않았습니다."),
        ("관련도 근거", s.get("relevance_reason")),
    ]
    for heading, body in sections:
        if body:
            parts.append(f'<section><h2>{heading}</h2><p>{e(body)}</p></section>')
    if p.get("unverified_numbers"):
        parts.append(f'<p class="warn">⚠ 근거 텍스트에서 확인되지 않은 수치: '
                     f'{e(", ".join(p["unverified_numbers"]))} — 원문 확인 필요</p>')
    if LEVEL_NOTE.get(level):
        parts.append(f'<p class="level-note">{e(LEVEL_NOTE[level])}</p>')
    if s.get("keywords"):
        parts.append('<p class="keywords">' + "".join(f"<span>#{e(k)}</span>" for k in s["keywords"]) + "</p>")
    parts.append(f'<p class="cta"><a class="button" href="{e(p.get("url"))}" target="_blank" rel="noopener">'
                 f'원문 보기 ↗</a></p>')
    meta = [f"게재 {p['published']}" if p.get("published") else None,
            f"요약 {(p.get('summarized_at') or '')[:10]}" if p.get("summarized_at") else None,
            f"DOI {p.get('doi')}"]
    parts.append(f'<p class="post-meta">{" · ".join(e(m) for m in meta if m)}</p>')
    parts.append("</article>")
    return "\n".join(parts)


# ---------------------------------------------------------------- 레이아웃

def page(title: str, site_title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header class="topbar"><div class="topbar-inner">
<a class="brand" href="index.html">{e(site_title)}</a>
<nav><a href="index.html">최근</a><a href="archive.html">날짜별</a></nav>
</div></header>
<main>
{body}
</main>
<footer>요약과 개요도는 AI가 논문의 초록·전문을 바탕으로 생성했습니다. 근거 표시를 확인하고 원문 링크로 검증하세요.</footer>
</body>
</html>
"""


def day_section(day: str, papers: list[dict], link_heading: bool) -> tuple[str, list[dict], list[dict]]:
    done = [p for p in papers if p.get("summary") is not None]
    pending = [p for p in papers if p.get("summary") is None]
    by_journal = Counter(p.get("journal_name") for p in papers)
    stats = " · ".join(f"{e(j)} {n}" for j, n in sorted(by_journal.items()))
    title = f'<a href="{e(day)}.html">{e(day)}</a>' if link_heading else e(day)
    head = (f'<div class="section-head"><h2>{title}</h2>'
            f'<p>{len(papers)}편 ({stats}) · 요약 {len(done)}편</p></div>')
    return head, done, pending


def build(data_dir: Path, out_dir: Path) -> list[str]:
    meta, papers = load(data_dir)
    site_title = meta.get("site_title", "Journal Daily Digest")
    by_day: dict[str, list[dict]] = defaultdict(list)
    for p in papers:
        by_day[p["digest_date"]].append(p)
    for ps in by_day.values():
        ps.sort(key=sort_key)
    days = sorted(by_day, reverse=True)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    shutil.copy2(HERE / "style.css", out_dir / "style.css")
    if (data_dir / "figures").is_dir():
        shutil.copytree(data_dir / "figures", out_dir / "figures")

    written = []

    def write(name: str, title: str, body: str) -> None:
        (out_dir / name).write_text(page(title, site_title, body), encoding="utf-8")
        written.append(name)

    # 첫 화면: 최근 날짜들
    hero = (f'<section class="hero"><h1>{e(site_title)}</h1>'
            f'<p>{e(" · ".join(meta.get("journals", {}).values()))}의 새 연구 논문을 매일 모아 쉽게 풀어 소개합니다.</p></section>')
    sections = []
    for day in days[:INDEX_DAYS]:
        head, done, pending = day_section(day, by_day[day], link_heading=True)
        body = card_grid(done) if done else '<p class="empty">아직 요약된 논문이 없습니다.</p>'
        if pending:
            body += f'<p class="more"><a href="{e(day)}.html">요약 대기 {len(pending)}편 보기 →</a></p>'
        sections.append(f'<section class="day">{head}{body}</section>')
    write("index.html", site_title,
          hero + ("".join(sections) or '<p class="empty">아직 수집된 논문이 없습니다.</p>'))

    # 날짜 페이지
    for i, day in enumerate(days):
        newer = days[i - 1] if i > 0 else None
        older = days[i + 1] if i + 1 < len(days) else None
        head, done, pending = day_section(day, by_day[day], link_heading=False)
        pager = ('<nav class="pager">'
                 + (f'<a href="{e(older)}.html">← {e(older)}</a>' if older else "<span></span>")
                 + (f'<a href="{e(newer)}.html">{e(newer)} →</a>' if newer else "<span></span>")
                 + "</nav>")
        body = (f'<section class="day">{head}'
                + (card_grid(done) if done else "")
                + pending_block(pending, open_=not done) + f"</section>{pager}")
        write(f"{day}.html", f"{day} · {site_title}", body)

    # 논문 상세
    for p in papers:
        if p.get("summary") is not None:
            s = p["summary"]
            write(paper_href(p), f"{s.get('title_ko') or p.get('title')} · {site_title}", render_article(p))

    # 날짜 목록
    rows = []
    for day in days:
        ps = by_day[day]
        done = sum(1 for p in ps if p.get("summary") is not None)
        top = max((p.get("relevance") or 0 for p in ps), default=0)
        rows.append(f'<tr><td><a href="{e(day)}.html">{e(day)}</a></td><td>{len(ps)}</td>'
                    f'<td>{done}</td><td>{e(top)}</td></tr>')
    write("archive.html", f"날짜별 · {site_title}",
          '<section class="day"><div class="section-head"><h2>날짜별</h2></div>'
          '<table class="archive"><thead><tr><th>날짜</th><th>논문</th><th>요약</th><th>최고 관련도</th>'
          '</tr></thead><tbody>' + "".join(rows) + "</tbody></table>"
          f'<p class="post-meta">빌드 {e(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))}</p></section>')
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=HERE.parent / "data")
    ap.add_argument("--out", type=Path, default=HERE.parent / "_site")
    args = ap.parse_args()
    written = build(args.data, args.out)
    print(f"{len(written)} pages → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
