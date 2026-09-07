"""Loads VeloCoder's built-in Help content (help/index.json + help/*.md)
and provides plain-text search over it. No Qt here at all -- HelpWindow
(help_window.py) is the only thing that turns this into rendered HTML.

Deliberately not full CommonMark -- these articles only ever use
headings, paragraphs, bullet lists, and **bold**, so a hand-rolled
subset avoids a new third-party dependency for something this small.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

HELP_DIR = Path(__file__).parent / "help"


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    category: str
    keywords: tuple[str, ...]
    body: str  # raw markdown-subset text


def load_topics() -> list[Topic]:
    """All topics, in the same category/topic order as index.json --
    that order is also this app's own presentation order (category
    navigation, "first topic shown on a fresh Help open"), not just a
    storage detail."""
    index = json.loads((HELP_DIR / "index.json").read_text())
    topics = []
    for category in index["categories"]:
        for entry in category["topics"]:
            body = (HELP_DIR / entry["file"]).read_text()
            topics.append(Topic(
                id=entry["id"],
                title=entry["title"],
                category=category["name"],
                keywords=tuple(entry.get("keywords", [])),
                body=body,
            ))
    return topics


def search_topics(topics: list[Topic], query: str) -> list[Topic]:
    """Case-insensitive substring match against title, keywords, and
    body text, in that priority order (a title match ranks above a
    keyword match, which ranks above a plain body-text match) -- ties
    within a tier keep index.json's own order. Returns every match, not
    just the best one: a search is meant to narrow the topic list down,
    not guess a single answer."""
    query = query.strip().lower()
    if not query:
        return list(topics)
    title_hits, keyword_hits, body_hits = [], [], []
    for topic in topics:
        if query in topic.title.lower():
            title_hits.append(topic)
        elif any(query in kw.lower() for kw in topic.keywords):
            keyword_hits.append(topic)
        elif query in topic.body.lower():
            body_hits.append(topic)
    return title_hits + keyword_hits + body_hits


def render_markdown_subset(text: str) -> str:
    """Headings (## ...), blank-line-separated paragraphs, "- " bullet
    lists, and **bold** -- exactly the subset every help/*.md file
    actually uses. Returns body HTML only (no <html>/<head>/<style> --
    help_window.py wraps that with theme-derived colors, since this
    module has no theme/Qt awareness at all)."""
    html_parts = []
    lines = text.strip().split("\n")
    i = 0
    paragraph_lines: list[str] = []
    list_items: list[str] = []

    def flush_paragraph():
        if paragraph_lines:
            html_parts.append(f"<p>{_inline(' '.join(paragraph_lines))}</p>")
            paragraph_lines.clear()

    def flush_list():
        if list_items:
            items = "".join(f"<li>{_inline(item)}</li>" for item in list_items)
            html_parts.append(f"<ul>{items}</ul>")
            list_items.clear()

    while i < len(lines):
        line = lines[i].strip()
        if not line:
            flush_paragraph()
            flush_list()
        elif line.startswith("## "):
            flush_paragraph()
            flush_list()
            html_parts.append(f"<h3>{_inline(line[3:])}</h3>")
        elif line.startswith("- "):
            flush_paragraph()
            list_items.append(line[2:])
        else:
            flush_list()
            paragraph_lines.append(line)
        i += 1
    flush_paragraph()
    flush_list()
    return "\n".join(html_parts)


def _inline(text: str) -> str:
    # **bold** -> <b>bold</b>. Applied after paragraph/list text is
    # already assembled, never before -- otherwise a "**" that happens
    # to straddle two source lines joined by flush_paragraph's own
    # " ".join() would never match as one token.
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
