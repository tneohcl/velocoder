"""Loads VeloCoder's built-in Help content (help/index.json + help/*.md)
and provides plain-text search over it. No Qt here at all -- HelpWindow
(help_window.py) is the only thing that turns this into rendered HTML.

Deliberately not full CommonMark -- these articles only ever use
headings, paragraphs, bullet lists, deck text, callouts, images, and
**bold**, so a hand-rolled subset avoids a new third-party dependency
for something this small.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

HELP_DIR = Path(__file__).parent / "help"
IMAGES_DIR = HELP_DIR / "images"

# ![alt](path) or ![alt](path "caption") -- path is resolved against
# IMAGES_DIR, never taken as a bare user-supplied filesystem path (see
# _render_image below).
_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(([^)"\s]+)(?:\s+"([^"]*)")?\)')
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


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


def render_markdown_subset(text: str, *, dark: bool = True) -> str:
    """Headings (## ...), blank-line-separated paragraphs, "- " bullet
    lists, "> " callouts, ![]() images, deck text, and **bold** --
    exactly the subset every help/*.md file actually uses. Returns body
    HTML only (no <html>/<head>/<style> -- help_window.py wraps that
    with theme-derived colors, since this module has no theme/Qt
    awareness at all).

    `dark` only ever reaches _render_image, to pick a screenshot's
    dark/light variant (see that function) -- everything else here
    really is theme-blind, matching the docstring above.

    Deck text: the article's own one-line summary, immediately under
    its title. Not a separate marker syntax -- it's just the first
    paragraph, when (and only when) that whole paragraph is a single
    **bold** span and nothing else, e.g.:

        **Choose how strongly VeloCoder compresses your video.**

        The rest of the article as normal paragraphs...

    A callout is one or more consecutive "> " lines; the first is the
    label (rendered bold, e.g. "> **Recommended**"), the rest is the
    callout's own body text, e.g.:

        > **Recommended**
        > Balanced works well for most videos.
    """
    html_parts = []
    lines = text.strip().split("\n")
    i = 0
    paragraph_lines: list[str] = []
    list_items: list[str] = []
    callout_lines: list[str] = []
    is_first_paragraph = True

    def flush_paragraph():
        nonlocal is_first_paragraph
        if paragraph_lines:
            joined = " ".join(paragraph_lines)
            deck_match = re.fullmatch(r"\*\*(.+)\*\*", joined.strip())
            if is_first_paragraph and deck_match:
                html_parts.append(f'<p class="deck">{_inline(deck_match.group(1), dark)}</p>')
            else:
                html_parts.append(f"<p>{_inline(joined, dark)}</p>")
            paragraph_lines.clear()
            is_first_paragraph = False

    def flush_list():
        if list_items:
            items = "".join(f"<li>{_inline(item, dark)}</li>" for item in list_items)
            html_parts.append(f"<ul>{items}</ul>")
            list_items.clear()

    def flush_callout():
        if callout_lines:
            label = _inline(callout_lines[0], dark)
            body = _inline(" ".join(callout_lines[1:]), dark) if len(callout_lines) > 1 else ""
            body_html = f'<div class="callout-body">{body}</div>' if body else ""
            html_parts.append(
                f'<div class="callout"><div class="callout-label">{label}</div>{body_html}</div>'
            )
            callout_lines.clear()

    while i < len(lines):
        line = lines[i].strip()
        if not line:
            flush_paragraph()
            flush_list()
            flush_callout()
        elif line.startswith("## "):
            flush_paragraph()
            flush_list()
            flush_callout()
            html_parts.append(f"<h3>{_inline(line[3:], dark)}</h3>")
        elif line.startswith("> "):
            flush_paragraph()
            flush_list()
            callout_lines.append(line[2:])
        elif line.startswith("- "):
            flush_paragraph()
            flush_callout()
            list_items.append(line[2:])
        else:
            flush_list()
            flush_callout()
            paragraph_lines.append(line)
        i += 1
    flush_paragraph()
    flush_list()
    flush_callout()
    return "\n".join(html_parts)


def _inline(text: str, dark: bool) -> str:
    # Images resolved first -- a caption or alt text containing "**"
    # would otherwise be mangled by the bold substitution running first.
    text = _IMAGE_RE.sub(lambda m: _render_image(m, dark), text)
    # **bold** -> <b>bold</b>. Applied after paragraph/list text is
    # already assembled, never before -- otherwise a "**" that happens
    # to straddle two source lines joined by flush_paragraph's own
    # " ".join() would never match as one token.
    return _BOLD_RE.sub(r"<b>\1</b>", text)


def _render_image(match: re.Match, dark: bool) -> str:
    alt, filename, caption = match.group(1), match.group(2), match.group(3)
    # An article's ![]() names a theme-neutral base (e.g.
    # "quick-start.png") -- resolved here against whichever themed
    # variant ("quick-start-dark.png"/"quick-start-light.png") actually
    # exists, so one line of markdown covers both without the article
    # itself knowing dark/light are different files. Falls back to the
    # bare name as given for any image that only ships one variant.
    base = Path(filename)
    themed_name = f"{base.stem}-{'dark' if dark else 'light'}{base.suffix}"
    # Resolved against IMAGES_DIR by filename only -- an article can
    # never point this at an arbitrary filesystem path (no "..", no
    # absolute path); Help content is a fixed, shipped asset set, not
    # user input, but there's no reason to trust path components from a
    # text file any further than that anyway.
    themed_path = (IMAGES_DIR / Path(themed_name).name).resolve()
    path = themed_path if themed_path.is_file() else (IMAGES_DIR / base.name).resolve()
    src = path.as_uri()
    html = f'<img class="help-image" src="{src}" alt="{alt}">'
    if caption:
        html += f'<div class="help-image-caption">{_inline(caption, dark)}</div>'
    return html
