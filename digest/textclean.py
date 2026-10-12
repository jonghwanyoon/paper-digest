"""JATS/HTML → 일반 텍스트. 표준 라이브러리 HTMLParser만 쓴다."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCK = {
    "p", "div", "section", "article", "li", "ul", "ol", "br", "tr", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "caption", "figure", "blockquote",
    "jats:p", "jats:sec", "jats:title", "jats:list-item", "jats:list",
    "sec", "sec-title", "list-item", "abstracttext",
}
_HEADING = {"h1", "h2", "h3", "h4", "h5", "h6", "jats:title", "sec-title"}
_SKIP = {"script", "style", "noscript", "button", "svg", "math", "nav", "form",
         "ref-list", "fn-group", "ack", "inline-formula", "disp-formula", "mml:math"}

# 요약 근거로 쓰지 않는 뒷부분 섹션(nature.com data-title, Europe PMC JATS 섹션 제목)
BACK_MATTER_SECTIONS = {
    "References", "Inline Recommendations", "Ethics declarations", "Additional information",
    "About this article", "Rights and permissions", "Author information", "Peer review",
    "Peer review information", "Acknowledgements", "Funding", "Supplementary information",
    "Supplementary Materials", "Source data", "Extended data", "Extended data figures and tables",
    "Code availability", "Data availability", "Data Availability Statement", "Associated Data",
    "Change history", "Comments", "Online content", "Author contributions", "Competing interests",
    "Footnotes", "Reporting summary",
}
_NOISE_LINES = {"Full size image", "Full size table", "Source data"}


def drop_sections(text: str, names: set[str]) -> str:
    """'## 제목' 으로 시작하는 섹션 중 names 에 든 것을 다음 '## ' 직전까지 지운다."""
    out, skipping = [], False
    for line in text.split("\n"):
        if line.startswith("## "):
            skipping = line[3:].strip() in names
        if not skipping:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


class _Extractor(HTMLParser):
    """sup → ^x, 인용 번호 링크 제거, 블록 경계 → 줄바꿈.

    sections=True 이면 data-title 이 있는 <section> 안의 텍스트만 섹션별로 모은다.
    """

    def __init__(self, sections: bool = False, skip_sections: set[str] | None = None):
        super().__init__(convert_charrefs=True)
        self.by_section = sections
        self.skip_sections = skip_sections or set()
        self.out: list[str] = []
        self.sec_stack: list[str | None] = []
        self.sections: list[tuple[str, list[str]]] = []
        self.skip_depth = 0
        self.in_cite_link = False
        self.sup_buffers: list[list[str]] = []

    # --- 출력 대상 결정
    def _target(self) -> list[str] | None:
        if self.skip_depth or self.in_cite_link:
            return None
        if self.sup_buffers:
            return self.sup_buffers[-1]
        if not self.by_section:
            return self.out
        titled = [t for t in self.sec_stack if t is not None]
        if not titled or any(t in self.skip_sections for t in titled):
            return None
        return self.sections[-1][1] if self.sections else None

    def _emit(self, s: str) -> None:
        t = self._target()
        if t is not None:
            t.append(s)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _SKIP:
            self.skip_depth += 1
            return
        if tag == "section":
            title = a.get("data-title")
            self.sec_stack.append(title)
            if title is not None and self.by_section and len([t for t in self.sec_stack if t]) == 1:
                self.sections.append((title, []))
        if tag == "a" and (a.get("data-test") == "citation-ref"
                           or a.get("data-track-action") == "reference anchor"):
            self.in_cite_link = True
            return
        if tag == "xref" and a.get("ref-type") == "bibr":
            self.in_cite_link = True
            return
        if tag in ("sup", "jats:sup"):
            self.sup_buffers.append([])
            return
        if tag in _BLOCK:
            self._emit("\n\n## " if tag in _HEADING else "\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag in ("a", "xref") and self.in_cite_link:
            self.in_cite_link = False
            return
        if tag in ("sup", "jats:sup") and self.sup_buffers:
            content = "".join(self.sup_buffers.pop()).strip()
            # 숫자만 든 sup 는 인용 번호일 수 있다: 숫자 바로 뒤(10^6)면 지수, 아니면 표식만 남겨
            # _normalize 에서 뒤에 원소 기호가 붙으면(²²⁹Th) 동위원소로 살리고 나머지는 지운다
            if content and (not re.fullmatch(r"[\d,\s–\-]+", content) or self._is_exponent_context()):
                self._emit("^" + content)
            elif content:
                self._emit(f"\x01{content}\x02")
            return
        if tag in _BLOCK:
            self._emit("\n")
        if tag == "section" and self.sec_stack:
            self.sec_stack.pop()

    def _is_exponent_context(self) -> bool:
        """10<sup>6</sup> 처럼 숫자 뒤의 숫자 sup 는 지수로 남긴다(인용 번호는 보통 단어 뒤)."""
        t = self._target()
        if not t:
            return False
        tail = "".join(t[-3:])
        return bool(re.search(r"\d\s*$|[×x]\s*10$", tail))

    def handle_data(self, data):
        self._emit(data)


def _normalize(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\x01(\d+)\x02(?=[A-Z])", r"^\1", text)
    text = re.sub(r"\x01[^\x02]*\x02", "", text)
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line in _NOISE_LINES:
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n(## )\n+", r"\n\1", text)
    text = re.sub(r"^## *$", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def markup_to_text(markup: str) -> str:
    # HTMLParser 는 <title> 을 RCDATA 로 다뤄 안쪽 태그(<sup> 등)를 글자로 남기므로 JATS title 은 이름을 바꾼다
    markup = re.sub(r"<(/?)title(?=[\s>])", r"<\1sec-title", markup)
    p = _Extractor()
    p.feed(markup)
    p.close()
    return _normalize("".join(p.out))


def inline_text(markup: str) -> str:
    """제목·저자 같은 한 줄 텍스트: 태그를 지우고 공백을 정리."""
    s = re.sub(r"<[^>]+>", "", markup or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def jats_abstract(jats: str) -> str:
    text = markup_to_text(jats)
    return re.sub(r"^## (Abstract|Summary)\s*\n", "", text).strip()


def nature_article(page: str) -> tuple[str | None, str | None]:
    """nature.com 논문 HTML → (초록, 전문). 비OA 페이지면 전문은 None."""
    p = _Extractor(sections=True, skip_sections=BACK_MATTER_SECTIONS)
    p.feed(page)
    p.close()
    abstract = None
    body_parts = []
    for title, chunks in p.sections:
        text = _normalize("".join(chunks))
        if not text:
            continue
        if title == "Abstract":
            abstract = re.sub(r"^## Abstract\s*\n?", "", text).strip()
        else:
            body_parts.append(text)
    body = "\n\n".join(body_parts)
    if len(body) < 3000:  # 비OA 페이지는 본문 섹션이 없다
        return abstract, None
    full = (f"## Abstract\n{abstract}\n\n" if abstract else "") + body
    return abstract, full


def openalex_abstract(inverted: dict[str, list[int]] | None) -> str | None:
    if not inverted:
        return None
    pos: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos)) or None
