#!/usr/bin/env python3
"""data/*.json → 정적 HTML(_site/). 표준 라이브러리만 쓰며 GitHub Actions 에서도 그대로 돈다.

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

SOURCE_LABEL = {"fulltext": "전문", "abstract": "초록", "pdf": "PDF"}
STATUS_NOTE = {
    "new": "근거 텍스트 확보 전",
    "awaiting_text": "초록·전문 확보 대기 중",
    "needs_pdf": "초록·전문을 구하지 못함 — PDF 필요",
    "ready": "요약 대기 중",
    "failed": "요약 실패",
}


def e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


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


_FIGURE_PATH = re.compile(r"figures/[A-Za-z0-9._-]+\.webp")


def render_figure(p: dict) -> str:
    """출판사 graphical abstract(출판사 서버에서 직접 표시)가 우선, 없으면 AI 개요도."""
    ga = p.get("graphical_abstract")
    if ga and ga.startswith("https://"):
        large = ga[:-4] + "_lrg.jpg" if ga.endswith("-ga1.jpg") else ga
        return (f'<figure class="fig ga"><a href="{e(large)}" target="_blank" rel="noopener">'
                f'<img src="{e(ga)}" alt="Graphical abstract" loading="lazy" referrerpolicy="no-referrer"></a>'
                f'<figcaption>Graphical abstract · © {e(p.get("journal_name"))} (출판사 이미지)</figcaption></figure>')
    fig = p.get("figure")
    if fig and _FIGURE_PATH.fullmatch(fig):
        model = f" · {p['figure_model']}" if p.get("figure_model") else ""
        return (f'<figure class="fig ai"><a href="{e(fig)}" target="_blank" rel="noopener">'
                f'<img src="{e(fig)}" alt="AI 생성 개요도" loading="lazy"></a>'
                f'<figcaption>AI 생성 개요도{e(model)} — 요약을 바탕으로 그린 그림이며 논문 원본 그림이 아닙니다. '
                f'세부 내용이 부정확할 수 있습니다.</figcaption></figure>')
    return ""


def render_paper(p: dict) -> str:
    """요약이 있는 논문 카드."""
    s = p["summary"]
    level = p.get("source_level")
    badges = [f'<span class="rel rel-{int(p.get("relevance") or 0)}">관련도 {e(p.get("relevance"))}</span>',
              f'<span class="badge journal">{e(p.get("journal_name"))}</span>']
    if p.get("article_type"):
        badges.append(f'<span class="badge">{e(p["article_type"])}</span>')
    if level:
        badges.append(f'<span class="badge src src-{e(level)}">근거: {e(SOURCE_LABEL.get(level, level))}</span>')
    if p.get("is_oa"):
        badges.append('<span class="badge oa">OA</span>')
    if p.get("pdf_pending"):
        badges.append('<span class="badge src-pdf">PDF 재요약 대기</span>')

    parts = ['<article class="paper">',
             f'<div class="badges">{"".join(badges)}</div>',
             f'<h2><a href="{e(p.get("url"))}" rel="noopener" target="_blank">{e(p.get("title"))}</a></h2>']
    if s.get("title_ko"):
        parts.append(f'<p class="title-ko">{e(s["title_ko"])}</p>')
    parts.append(f'<p class="authors">{e(p.get("authors"))}</p>')
    parts.append(f'<p class="tldr">{e(s.get("tldr"))}</p>')
    figure = render_figure(p)
    if figure:
        parts.append(figure)
    if s.get("summary"):
        parts.append(f'<p class="summary">{e(s["summary"])}</p>')
    details = [f"<dt>기존 연구와의 차별점</dt><dd>{e(s.get('novelty') or '근거 텍스트에 기존 연구와의 비교가 없습니다.')}</dd>",
               f"<dt>한계</dt><dd>{e(s.get('limitations') or '근거 텍스트에 한계가 언급되지 않았습니다.')}</dd>"]
    if s.get("relevance_reason"):
        details.append(f"<dt>관련도 근거</dt><dd>{e(s['relevance_reason'])}</dd>")
    if details:
        parts.append(f'<dl class="details">{"".join(details)}</dl>')
    if s.get("keywords"):
        parts.append('<p class="keywords">' + " ".join(f"<span>{e(k)}</span>" for k in s["keywords"]) + "</p>")
    if p.get("unverified_numbers"):
        nums = ", ".join(p["unverified_numbers"])
        parts.append(f'<p class="warn">⚠ 근거 텍스트에서 확인되지 않은 수치: {e(nums)} — 원문 확인 필요</p>')
    level_note = {"abstract": "초록만 보고 작성한 요약입니다. 방법·수치는 원문에서 확인하세요."}.get(level)
    if level_note:
        parts.append(f'<p class="level-note">{e(level_note)}</p>')
    meta = [p.get("engine"), p.get("model"), (p.get("summarized_at") or "")[:10], p.get("doi")]
    parts.append(f'<p class="meta">{" · ".join(e(m) for m in meta if m)}</p>')
    parts.append("</article>")
    return "\n".join(parts)


def page(title: str, site_title: str, body: str, nav: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header><a class="site" href="index.html">{e(site_title)}</a><nav>{nav}</nav></header>
<main>
{body}
</main>
<footer>요약은 LLM이 메타데이터·초록·전문을 바탕으로 생성했습니다. 근거 표시를 확인하고 원문 링크로 검증하세요.</footer>
</body>
</html>
"""


def render_pending(p: dict) -> str:
    note = STATUS_NOTE.get(p.get("status"), p.get("status"))
    return (f'<li><span class="badge journal">{e(p.get("journal_name"))}</span> '
            f'<a href="{e(p.get("url"))}" rel="noopener" target="_blank">{e(p.get("title"))}</a> '
            f'<span class="note status-{e(p.get("status"))}">{e(note)}</span></li>')


def day_body(date: str, papers: list[dict]) -> str:
    papers = sorted(papers, key=sort_key)
    done = [p for p in papers if p.get("summary") is not None]
    pending = [p for p in papers if p.get("summary") is None]
    by_journal = Counter(p.get("journal_name") for p in papers)
    stats = " · ".join(f"{e(j)} {n}" for j, n in sorted(by_journal.items()))
    parts = [f'<section class="day-head"><h1>{e(date)} 새 논문</h1>'
             f'<p>{len(papers)}편 ({stats}) · 요약 완료 {len(done)}편 · 관련도 순</p></section>']
    parts += [render_paper(p) for p in done]
    if pending:
        parts.append(f'<details class="pending-list"{"" if done else " open"}>'
                     f'<summary>아직 요약 전인 논문 {len(pending)}편</summary><ul>'
                     + "".join(render_pending(p) for p in pending) + "</ul></details>")
    return "\n".join(parts)


def build(data_dir: Path, out_dir: Path) -> list[str]:
    meta, papers = load(data_dir)
    site_title = meta.get("site_title", "Journal Daily Digest")
    by_day: dict[str, list[dict]] = defaultdict(list)
    for p in papers:
        by_day[p["digest_date"]].append(p)
    days = sorted(by_day, reverse=True)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    shutil.copy2(HERE / "style.css", out_dir / "style.css")
    if (data_dir / "figures").is_dir():
        shutil.copytree(data_dir / "figures", out_dir / "figures")

    written = []
    for i, day in enumerate(days):
        newer = days[i - 1] if i > 0 else None
        older = days[i + 1] if i + 1 < len(days) else None
        nav = "".join([
            f'<a href="{e(older)}.html">← {e(older)}</a>' if older else "",
            '<a href="archive.html">날짜 목록</a>',
            f'<a href="{e(newer)}.html">{e(newer)} →</a>' if newer else "",
        ])
        html_text = page(f"{day} · {site_title}", site_title, day_body(day, by_day[day]), nav)
        (out_dir / f"{day}.html").write_text(html_text, encoding="utf-8")
        written.append(f"{day}.html")
        if i == 0:
            (out_dir / "index.html").write_text(html_text, encoding="utf-8")
            written.append("index.html")

    if not days:
        (out_dir / "index.html").write_text(
            page(site_title, site_title, "<p>아직 수집된 논문이 없습니다.</p>", ""), encoding="utf-8")
        written.append("index.html")

    rows = []
    for day in days:
        ps = by_day[day]
        done = sum(1 for p in ps if p.get("summary") is not None)
        top = max((p.get("relevance") or 0 for p in ps), default=0)
        rows.append(f'<tr><td><a href="{e(day)}.html">{e(day)}</a></td><td>{len(ps)}</td>'
                    f'<td>{done}</td><td>{e(top)}</td></tr>')
    archive = ('<h1>날짜 목록</h1><table class="archive"><thead><tr><th>날짜</th><th>논문</th>'
               '<th>요약</th><th>최고 관련도</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>"
               f'<p class="meta">빌드: {e(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))}</p>')
    (out_dir / "archive.html").write_text(
        page(f"날짜 목록 · {site_title}", site_title, archive, '<a href="index.html">최신</a>'),
        encoding="utf-8")
    written.append("archive.html")
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
