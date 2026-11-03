"""A small static-site generator for drive/published/*.md.

Output (drive/site/): index.html, one page per note under posts/, feed.xml (RSS 2.0) and
style.css. Markdown is rendered with raw HTML disabled, so notes cannot inject scripts.
The build is atomic: a new site replaces the old one only when complete.
"""

from __future__ import annotations

import ast
import html
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from markdown_it import MarkdownIt

_FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": True}).enable(
    "table"
)

STYLE = """\
:root { --bg: #fbfaf7; --fg: #1d1d1f; --muted: #6b6b70; --accent: #c2410c; --rule: #e7e5e0;
        --code: #f1efe9; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #141416; --fg: #e9e7e2; --muted: #9a9aa0; --accent: #fb923c; --rule: #2a2a2e;
          --code: #1e1e22; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 17px/1.65 ui-serif, Georgia, "Times New Roman", serif; }
main, header, footer { max-width: 42rem; margin: 0 auto; padding: 0 1rem; }
header { padding-top: 2.5rem; }
header a { color: var(--fg); text-decoration: none; }
header h1 { margin: 0; font-size: 1.6rem; }
header p { margin: .25rem 0 0; color: var(--muted); font-style: italic; }
a { color: var(--accent); }
h1, h2, h3 { line-height: 1.25; font-family: ui-sans-serif, system-ui, sans-serif; }
article h1 { font-size: 2rem; margin-bottom: .25rem; }
.meta { color: var(--muted); font-size: .9rem; font-family: ui-sans-serif, system-ui; }
.tag { display: inline-block; margin-right: .4rem; }
ul.posts { list-style: none; padding: 0; }
ul.posts li { padding: .9rem 0; border-bottom: 1px solid var(--rule); }
ul.posts a { font-size: 1.15rem; text-decoration: none; }
pre, code { background: var(--code); font: .88em/1.5 ui-monospace, Menlo, monospace; }
pre { padding: .8rem 1rem; overflow-x: auto; border-radius: 6px; }
code { padding: .1em .3em; border-radius: 4px; }
pre code { padding: 0; }
blockquote { margin: 0; padding-left: 1rem; border-left: 3px solid var(--rule);
             color: var(--muted); }
table { border-collapse: collapse; } td, th { border: 1px solid var(--rule); padding: .3rem .6rem; }
img { max-width: 100%; }
footer { margin: 3rem auto 2rem; color: var(--muted); font-size: .85rem;
         font-family: ui-sans-serif, system-ui; border-top: 1px solid var(--rule);
         padding-top: 1rem; }
"""


@dataclass
class Note:
    slug: str
    title: str
    date: str
    tags: list[str]
    author: str
    body_md: str

    @property
    def html(self) -> str:
        return _md.render(self.body_md)

    @property
    def published(self) -> datetime:
        try:
            return datetime.fromisoformat(self.date).replace(tzinfo=UTC)
        except ValueError:
            return datetime.now(UTC)


@dataclass
class SiteResult:
    path: Path
    notes: list[Note] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


def _value(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        try:
            return str(ast.literal_eval(raw))
        except (ValueError, SyntaxError):
            return raw.strip("'\"")
    return raw


def parse_note(path: Path) -> Note:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = text
    match = _FRONT_RE.match(text)
    if match:
        body = text[match.end() :]
        for line in match.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip().lower()] = value.strip()
    tags_raw = meta.get("tags", "").strip("[]")
    tags = [_value(t) for t in tags_raw.split(",") if t.strip()]
    title = _value(meta.get("title", "")) or path.stem
    # Drop a leading H1 that repeats the title (the page template prints the title).
    lines = body.lstrip().splitlines()
    if lines and lines[0].lstrip("# ").strip() == title.strip():
        body = "\n".join(lines[1:])
    return Note(
        slug=path.stem,
        title=title,
        date=_value(meta.get("date", "")) or path.stem[:10],
        tags=tags,
        author=_value(meta.get("author", "")),
        body_md=body.strip() + "\n",
    )


def _page(site_title: str, tagline: str, title: str, body: str, *, root: str, footer: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="{root}style.css">
<link rel="alternate" type="application/rss+xml" title="{html.escape(site_title)}"
      href="{root}feed.xml">
</head>
<body>
<header><a href="{root}index.html"><h1>{html.escape(site_title)}</h1></a>
<p>{html.escape(tagline)}</p></header>
<main>
{body}
</main>
<footer>{footer}</footer>
</body>
</html>
"""


def build_site(
    drive: Path,
    *,
    title: str,
    description: str,
    author: str,
    base_url: str | None = None,
) -> SiteResult:
    source = drive / "published"
    target = drive / "site"
    staging = drive / ".site-build"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "posts").mkdir(parents=True)

    notes = (
        [parse_note(p) for p in sorted(source.glob("*.md")) if p.name != "index.md"]
        if source.exists()
        else []
    )
    notes.sort(key=lambda n: (n.date, n.slug), reverse=True)
    footer = (
        f"Written autonomously by {html.escape(author)}, an AI agent built on "
        '<a href="https://github.com/ThePeeps191/decapod">Decapod</a>. '
        '<a href="feed.xml">RSS</a>'
    )
    files: list[str] = []

    def write(rel: str, content: str) -> None:
        (staging / rel).write_text(content, encoding="utf-8", newline="\n")
        files.append(rel)

    write("style.css", STYLE)
    items = []
    for note in notes:
        tags = "".join(f'<span class="tag">#{html.escape(t)}</span>' for t in note.tags)
        body = (
            f"<article><h1>{html.escape(note.title)}</h1>"
            f'<p class="meta">{html.escape(note.date)} {tags}</p>\n{note.html}</article>'
            '<p><a href="../index.html">← All notes</a></p>'
        )
        write(
            f"posts/{note.slug}.html",
            _page(
                title,
                description,
                f"{note.title} · {title}",
                body,
                root="../",
                footer=footer.replace('href="feed.xml"', 'href="../feed.xml"'),
            ),
        )
        items.append(
            f'<li><a href="posts/{note.slug}.html">{html.escape(note.title)}</a>'
            f'<div class="meta">{html.escape(note.date)} {tags}</div></li>'
        )
    listing = (
        ('<ul class="posts">' + "".join(items) + "</ul>")
        if items
        else "<p>Nothing published yet.</p>"
    )
    write("index.html", _page(title, description, title, listing, root="", footer=footer))

    base = (base_url or "").rstrip("/")
    feed_items = []
    for note in notes[:50]:
        link = f"{base}/posts/{note.slug}.html" if base else f"posts/{note.slug}.html"
        feed_items.append(
            "<item>"
            f"<title>{xml_escape(note.title)}</title>"
            f"<link>{xml_escape(link)}</link>"
            f'<guid isPermaLink="{"true" if base else "false"}">{xml_escape(link)}</guid>'
            f"<pubDate>{format_datetime(note.published)}</pubDate>"
            f"<description>{xml_escape(note.html)}</description>"
            "</item>"
        )
    write(
        "feed.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
        f"<title>{xml_escape(title)}</title>"
        f"<link>{xml_escape(base + '/' if base else '')}</link>"
        f"<description>{xml_escape(description)}</description>"
        f"<generator>Decapod</generator>{''.join(feed_items)}</channel></rss>\n",
    )

    old = drive / ".site-old"
    if old.exists():
        shutil.rmtree(old)
    if target.exists():
        target.rename(old)
    staging.rename(target)
    if old.exists():
        shutil.rmtree(old)
    return SiteResult(path=target, notes=notes, files=sorted(files))
