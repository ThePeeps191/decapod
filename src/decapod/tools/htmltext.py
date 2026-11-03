"""Readable text extraction from HTML using only the standard library."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

_SKIP = {
    "script",
    "style",
    "noscript",
    "svg",
    "template",
    "iframe",
    "canvas",
    "form",
    "button",
    "select",
    "nav",
    "footer",
    "aside",
}
_BLOCK = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "header",
    "br",
    "li",
    "ul",
    "ol",
    "tr",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "pre",
    "blockquote",
    "hr",
    "dd",
    "dt",
    "figure",
    "figcaption",
}
_VOID = {
    "br",
    "hr",
    "img",
    "input",
    "meta",
    "link",
    "area",
    "base",
    "col",
    "embed",
    "source",
    "track",
    "wbr",
}


class _Extractor(HTMLParser):
    def __init__(self, base_url: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._in_title = False
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            if tag not in _VOID:
                self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if self._skip_depth:
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.parts.append("#" * int(tag[1]) + " ")
        if tag == "li":
            self.parts.append("- ")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("javascript:", "#", "mailto:")):
                self._href = urljoin(self.base_url, href) if self.base_url else href
                self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and tag not in _VOID:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if self._skip_depth:
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._href:
            text = " ".join("".join(self._link_text).split())
            if text and len(self.links) < 200:
                self.links.append((text, self._href))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth:
            return
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data)


def html_to_text(
    markup: str, base_url: str | None = None
) -> tuple[str, str, list[tuple[str, str]]]:
    """Return (title, text, links)."""
    # A truncated page can end inside a tag; parsers differ on whether that tail becomes
    # text (it does on Python < 3.13), so drop it up front for consistent output.
    last_open = markup.rfind("<")
    if last_open > markup.rfind(">"):
        markup = markup[:last_open]
    parser = _Extractor(base_url)
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed markup: keep what we have
        pass
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    title = " ".join("".join(parser.title_parts).split())
    return title, text, parser.links
