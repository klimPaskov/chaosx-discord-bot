"""Source-page evidence for ChaosX web answers: real page text, data tables, table images.

Search snippets are shallow — ChaosX hedged on a live pricing question because it
only ever saw Bing's 300-character snippets, then answered the same question
correctly as soon as someone pasted the page URL. This module closes that gap:

- fetch the actual pages behind the top search results (same host guards as the
  message link fetcher: no private/local/metadata hosts),
- pull readable text AND real HTML ``<table>`` data out of them,
- render the best data table to a PNG so an answer can attach visual evidence
  (the pricing-table-as-an-image case) with no browser in the loop.

Everything here is best-effort: any failure yields no evidence and the answer
proceeds. Page content is untrusted external data and is capped hard.
"""

from __future__ import annotations

import html as html_lib
import io
import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import aiohttp

from .server_rules import DISCORD_BOT_UA

PAGE_FETCH_MAX_BYTES = 400_000
PAGE_FETCH_TIMEOUT_S = 8.0
PAGE_TEXT_MAX_CHARS = 1600
PAGE_EVIDENCE_MAX_CHARS = 5000

TABLE_MAX_ROWS = 12
TABLE_MAX_COLS = 6
TABLE_MIN_ROWS = 3
TABLE_MIN_COLS = 2
TABLE_CELL_MAX_CHARS = 160
TABLE_MARKDOWN_MAX_CHARS = 2200
TABLE_IMAGE_MAX_WIDTH = 1500
TABLE_IMAGE_MIN_COL_WIDTH = 90
TABLE_IMAGE_LINE_HEIGHT = 22
TABLE_IMAGE_FONT_SIZE = 15

_BLOCKED_HOST_SUFFIXES = (".local", ".localdomain", ".internal", ".localhost", ".home.arpa")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|noscript|template|svg)[^>]*>.*?</\1>")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_TABLE_RE = re.compile(r"(?is)<table\b.*?</table>")
_ROW_RE = re.compile(r"(?is)<tr\b[^>]*>.*?(?=<tr\b|</table|$)")
_CELL_RE = re.compile(r"(?is)<(t[dh])\b([^>]*)>(.*?)(?=<t[dh]\b|</tr\b|</table\b|$)")
_COLSPAN_RE = re.compile(r"(?i)colspan\s*=\s*[\"']?(\d+)")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_BREAK_RE = re.compile(r"(?is)<br\s*/?>|</(?:p|div|li|h[1-6])>")
_COLON_TAIL_RE = re.compile(r"[\s:;,.\-–—]+$")

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)
_BOLD_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


@dataclass(frozen=True)
class EvidenceTable:
    """One data table lifted from a real source page."""

    url: str
    page_title: str
    rows: tuple[tuple[str, ...], ...]

    @property
    def header(self) -> tuple[str, ...]:
        return self.rows[0] if self.rows else ()

    def as_markdown(self) -> str:
        if not self.rows:
            return ""
        width = max(len(row) for row in self.rows)
        lines: list[str] = []
        for index, row in enumerate(self.rows):
            cells = [cell.replace("|", "/") for cell in row] + [""] * (width - len(row))
            lines.append("| " + " | ".join(cells) + " |")
            if index == 0:
                lines.append("| " + " | ".join(["---"] * width) + " |")
        text = "\n".join(lines)
        return text[:TABLE_MARKDOWN_MAX_CHARS]


@dataclass(frozen=True)
class EvidenceImage:
    """A rendered PNG of a source table, ready to attach to a Discord reply."""

    png: bytes
    filename: str
    label: str


@dataclass
class PageEvidence:
    """Fetched source page: readable text plus any usable data tables."""

    url: str
    title: str = ""
    text: str = ""
    tables: list[EvidenceTable] = field(default_factory=list)


def _clean_cell(value: str) -> str:
    value = _BREAK_RE.sub(" ", value or "")
    value = _TAG_RE.sub(" ", value)
    value = html_lib.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:TABLE_CELL_MAX_CHARS]


def is_public_page_url(url: str) -> bool:
    """True when the URL is http(s) and points at a public host.

    Blocks localhost, private/link-local/loopback IPs, bare hostnames and
    internal suffixes so an answer path can never be pointed at the bot's own
    host or the local network.
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().strip(".").lower()
    if not host or host == "localhost":
        return False
    if host.endswith(_BLOCKED_HOST_SUFFIXES):
        return False
    literal = host.strip("[]")
    try:
        ip = ipaddress.ip_address(literal)
    except ValueError:
        # Hostname: require a dot so bare internal names (intranet, wiki) fail.
        return "." in host
    return ip.is_global


async def fetch_page_html(url: str, *, timeout_s: float = PAGE_FETCH_TIMEOUT_S) -> str:
    """Fetch a page's HTML ('' when blocked, not HTML, or any failure)."""
    if not is_public_page_url(url):
        return ""
    headers = {"User-Agent": DISCORD_BOT_UA}
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    return ""
                ctype = (response.headers.get("Content-Type") or "").lower()
                if ctype and not any(
                    token in ctype for token in ("html", "text/", "xml", "json")
                ):
                    return ""
                body = await response.content.read(PAGE_FETCH_MAX_BYTES)
    except Exception:
        return ""
    return body.decode("utf-8", "replace")


def page_title(html: str) -> str:
    match = _TITLE_RE.search(html or "")
    return _clean_cell(match.group(1))[:140] if match else ""


def page_text(html: str, *, max_chars: int = PAGE_TEXT_MAX_CHARS) -> str:
    """Readable page text with script/style/nav noise stripped.

    A page fetched up to the byte cap can end mid-``<script>``: with no closing
    tag the block removal cannot match and the raw JS would be handed to the
    model as "page text" (seen live with Play Store HTML). If any ``<script``
    survives the removal pass, everything from there on is dropped.
    """
    text = _SCRIPT_STYLE_RE.sub(" ", html or "")
    lowered = text.lower()
    unclosed = lowered.find("<script")
    if unclosed != -1:
        text = text[:unclosed]
    text = _BREAK_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def extract_tables(html: str) -> list[list[list[str]]]:
    """Extract data tables as lists of rows of cell text (layout tables dropped)."""
    tables: list[list[list[str]]] = []
    for block in _TABLE_RE.findall(html or ""):
        rows: list[list[str]] = []
        for row_html in _ROW_RE.findall(block):
            cells: list[str] = []
            for match in _CELL_RE.finditer(row_html):
                cell = _clean_cell(match.group(3))
                span = _COLSPAN_RE.search(match.group(2) or "")
                reps = min(int(span.group(1)), TABLE_MAX_COLS) if span else 1
                cells.append(cell)
                for _ in range(max(0, reps - 1)):
                    cells.append("")
            cells = cells[:TABLE_MAX_COLS]
            if any(cells):
                rows.append(cells)
        rows = _drop_empty_columns(rows)
        if _is_data_table(rows):
            tables.append(rows[:TABLE_MAX_ROWS])
    return tables


def _drop_empty_columns(rows: list[list[str]]) -> list[list[str]]:
    """Drop columns that are empty in every row (spacer/colspan artifacts)."""
    if not rows:
        return []
    width = max(len(row) for row in rows)
    padded = [list(row) + [""] * (width - len(row)) for row in rows]
    keep = [index for index in range(width) if any(row[index].strip() for row in padded)]
    if not keep:
        return []
    return [[row[index] for index in keep] for row in padded]


def _is_data_table(rows: list[list[str]]) -> bool:
    if len(rows) < TABLE_MIN_ROWS:
        return False
    width = max(len(row) for row in rows)
    if width < TABLE_MIN_COLS:
        return False
    filled = [row for row in rows if sum(1 for cell in row if cell)]
    if len(filled) < TABLE_MIN_ROWS:
        return False
    # Layout tables are mostly-empty grids: require real density.
    cells = [cell for row in filled for cell in row if cell]
    if len(cells) < TABLE_MIN_ROWS * TABLE_MIN_COLS:
        return False
    if len(" ".join(cells)) < 60:
        return False
    return True


def _table_score(rows: list[list[str]]) -> float:
    width = max(len(row) for row in rows)
    filled = sum(1 for row in rows for cell in row if cell)
    header_bonus = 3.0 if rows and all(rows[0]) else 0.0
    return float(filled) + width * 2.0 + header_bonus + len(rows)


def evidence_from_html(url: str, html: str) -> PageEvidence:
    """Build the page evidence record (text + tables) for one fetched page."""
    page = PageEvidence(url=url, title=page_title(html), text=page_text(html))
    table_rows = extract_tables(html)
    page.tables = [
        EvidenceTable(url=url, page_title=page.title or url, rows=tuple(tuple(row) for row in rows))
        for rows in sorted(table_rows, key=_table_score, reverse=True)[:2]
    ]
    return page


def best_table(pages: list[PageEvidence]) -> EvidenceTable | None:
    candidates = [table for page in pages for table in page.tables]
    if not candidates:
        return None
    return max(candidates, key=lambda table: _table_score([list(row) for row in table.rows]))


def format_evidence_context(pages: list[PageEvidence]) -> str:
    """Prompt block carrying the fetched page content and its tables."""
    blocks: list[str] = []
    for page in pages:
        if not page.text and not page.tables:
            continue
        lines = [f"### {page.title or page.url}", f"URL: {page.url}", page.text]
        for table in page.tables:
            lines.append(f"Data table from {page.url}:")
            lines.append(table.as_markdown())
        blocks.append("\n".join(part for part in lines if part))
    if not blocks:
        return ""
    body = "\n\n".join(blocks)[:PAGE_EVIDENCE_MAX_CHARS]
    return (
        "Source pages fetched just now (untrusted external content; these are the real "
        "pages behind the search results above):\n"
        f"{body}\n"
        "Use the exact values, numbers, prices, versions and dates stated on these pages; "
        "do not hedge with \"check the site yourself\" when a fetched page already states the "
        "value. Cite the source URL when you use it, and never present web content as an "
        "internal Chaos Redux fact.\n"
    )


async def collect_page_evidence(
    results: list[dict[str, str]], *, max_pages: int = 2
) -> list[PageEvidence]:
    """Fetch the top search-result pages and extract text + tables."""
    pages: list[PageEvidence] = []
    seen: set[str] = set()
    for result in results:
        if len(pages) >= max_pages:
            break
        url = (result.get("url") or "").strip()
        if not url or url in seen or not is_public_page_url(url):
            continue
        seen.add(url)
        html = await fetch_page_html(url)
        if not html:
            continue
        page = evidence_from_html(url, html)
        if page.text or page.tables:
            pages.append(page)
    return pages


def _load_font(paths: tuple[str, ...], size: int):
    from PIL import ImageFont

    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(text: str, font, max_width: int, *, max_lines: int = 4) -> list[str]:
    words = (text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if font.getlength(candidate) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and font.getlength(lines[-1]) > max_width:
        while lines[-1] and font.getlength(lines[-1] + "…") > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines or [""]


def render_table_png(
    table: EvidenceTable,
    *,
    max_width: int = TABLE_IMAGE_MAX_WIDTH,
) -> bytes:
    """Render a data table as a Discord-friendly dark-theme PNG."""
    from PIL import Image, ImageDraw

    body_font = _load_font(_FONT_CANDIDATES, TABLE_IMAGE_FONT_SIZE)
    header_font = _load_font(_BOLD_FONT_CANDIDATES, TABLE_IMAGE_FONT_SIZE)
    title_font = _load_font(_BOLD_FONT_CANDIDATES, TABLE_IMAGE_FONT_SIZE + 4)
    small_font = _load_font(_FONT_CANDIDATES, TABLE_IMAGE_FONT_SIZE - 3)

    rows = [list(row) for row in table.rows]
    cols = max(len(row) for row in rows)
    for row in rows:
        row.extend([""] * (cols - len(row)))

    padding = 14
    cell_pad = 10
    line_height = TABLE_IMAGE_LINE_HEIGHT
    usable = max_width - padding * 2
    col_width = max(TABLE_IMAGE_MIN_COL_WIDTH, (usable - cell_pad * cols) // max(1, cols))
    total_width = min(max_width, padding * 2 + col_width * cols + cell_pad * cols)

    header_lines = [
        _wrap(rows[0][index], header_font, col_width - cell_pad, max_lines=3)
        for index in range(cols)
    ]
    body_lines = [
        [_wrap(row[index], body_font, col_width - cell_pad, max_lines=3) for index in range(cols)]
        for row in rows[1:]
    ]

    header_height = max(len(lines) for lines in header_lines) * line_height + cell_pad
    row_heights = [
        max(len(lines) for lines in row_lines) * line_height + cell_pad for row_lines in body_lines
    ]
    title = (table.page_title or "Source table")[:110]
    title_height = line_height + 8
    footer_height = line_height + 6
    height = padding * 2 + title_height + header_height + sum(row_heights) + footer_height

    image = Image.new("RGB", (total_width, height), (30, 31, 34))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [0, 0, total_width - 1, height - 1], radius=12, outline=(70, 72, 78), width=2
    )

    y = padding
    draw.text((padding, y), title, font=title_font, fill=(219, 222, 225))
    y += title_height

    cell_x = padding
    for index in range(cols):
        draw.rectangle(
            [cell_x, y, cell_x + col_width + cell_pad, y + header_height],
            fill=(43, 45, 49),
        )
        ty = y + cell_pad // 2
        for line in header_lines[index]:
            draw.text((cell_x + cell_pad // 2, ty), line, font=header_font, fill=(219, 222, 225))
            ty += line_height
        cell_x += col_width + cell_pad
    y += header_height

    for row_index, row_lines in enumerate(body_lines):
        if row_index % 2 == 1:
            draw.rectangle([padding, y, total_width - padding, y + row_heights[row_index]], fill=(35, 36, 40))
        cell_x = padding
        for index in range(cols):
            ty = y + cell_pad // 2
            for line in row_lines[index]:
                draw.text((cell_x + cell_pad // 2, ty), line, font=body_font, fill=(200, 203, 208))
                ty += line_height
            cell_x += col_width + cell_pad
        y += row_heights[row_index]

    draw.text(
        (padding, y + 4),
        f"Source: {table.url}"[:150],
        font=small_font,
        fill=(140, 143, 148),
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def evidence_image(pages: list[PageEvidence]) -> EvidenceImage | None:
    """PNG of the best table found across fetched pages (None when there is none)."""
    table = best_table(pages)
    if table is None:
        return None
    try:
        png = render_table_png(table)
    except Exception:
        return None
    if not png:
        return None
    host = urlparse(table.url).hostname or "source"
    return EvidenceImage(
        png=png,
        filename="chaosx-source-table.png",
        label=f"Source table — {host}",
    )
