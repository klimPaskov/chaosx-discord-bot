"""Source-page evidence: real page text, data tables and table images."""

import pytest

from chaosx_bot.web_grounding import WebGrounder
from chaosx_bot.web_sources import (
    PageEvidence,
    best_table,
    collect_page_evidence,
    evidence_from_html,
    evidence_image,
    extract_tables,
    format_evidence_context,
    is_public_page_url,
    page_text,
    page_title,
    render_table_png,
)

PRICING_HTML = """
<html><head><title>DeepSeek V4.1 Flash — Example Host</title>
<script>var noise = "should not appear in page text";</script></head>
<body>
<nav><table><tr><td>menu</td></tr></table></nav>
<h1>Pricing</h1>
<table>
  <tr><th>Model</th><th>Input / 1M</th><th>Output / 1M</th></tr>
  <tr><td>flash base</td><td>$0.30</td><td>$1.20</td></tr>
  <tr><td>flash peak</td><td>$0.60</td><td>$2.40</td></tr>
  <tr><td>pro base</td><td>$1.32</td><td>$3.96</td></tr>
</table>
<table><tr><td>one column only</td></tr><tr><td>not a data table</td></tr></table>
</body></html>
"""


def test_public_page_url_blocks_private_and_local_hosts():
    assert is_public_page_url("https://example.com/pricing")
    assert is_public_page_url("http://93.184.216.34/page")
    assert not is_public_page_url("http://localhost:8787/secret")
    assert not is_public_page_url("http://127.0.0.1/")
    assert not is_public_page_url("http://10.0.0.5/admin")
    assert not is_public_page_url("http://192.168.1.1/")
    assert not is_public_page_url("http://169.254.169.254/latest/meta-data/")
    assert not is_public_page_url("http://intranet/wiki")
    assert not is_public_page_url("http://example.local/")
    assert not is_public_page_url("ftp://example.com/file")
    assert not is_public_page_url("")


def test_page_text_and_title_strip_scripts():
    assert page_title(PRICING_HTML) == "DeepSeek V4.1 Flash — Example Host"
    text = page_text(PRICING_HTML)
    assert "should not appear" not in text
    assert "Pricing" in text


def test_extract_tables_keeps_only_data_tables():
    tables = extract_tables(PRICING_HTML)
    assert len(tables) == 1
    rows = tables[0]
    assert rows[0] == ["Model", "Input / 1M", "Output / 1M"]
    assert ["flash peak", "$0.60", "$2.40"] in rows
    assert len(rows) == 4


def test_extract_tables_drops_empty_columns():
    html = """
    <table>
      <tr><th>Model name</th><th></th><th>Input price per 1M tokens</th></tr>
      <tr><td>flash base</td><td></td><td>$0.30 USD</td></tr>
      <tr><td>pro base</td><td></td><td>$1.32 USD</td></tr>
      <tr><td>max base</td><td></td><td>$3.96 USD</td></tr>
    </table>
    """
    tables = extract_tables(html)
    assert len(tables) == 1
    assert tables[0][0] == ["Model name", "Input price per 1M tokens"]
    assert tables[0][2] == ["pro base", "$1.32 USD"]


def test_evidence_from_html_builds_markdown_and_table_records():
    page = evidence_from_html("https://example.com/pricing", PRICING_HTML)
    assert page.url == "https://example.com/pricing"
    table = best_table([page])
    assert table is not None
    markdown = table.as_markdown()
    assert markdown.startswith("| Model | Input / 1M | Output / 1M |")
    assert "| --- | --- | --- |" in markdown
    assert "$3.96" in markdown


def test_render_table_png_produces_png_bytes():
    page = evidence_from_html("https://example.com/pricing", PRICING_HTML)
    table = best_table([page])
    assert table is not None
    png = render_table_png(table)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 2000


def test_evidence_image_labels_source_host_and_needs_a_table():
    page = evidence_from_html("https://example.com/pricing", PRICING_HTML)
    image = evidence_image([page])
    assert image is not None
    assert image.filename == "chaosx-source-table.png"
    assert "example.com" in image.label
    assert image.png[:4] == b"\x89PNG"
    assert evidence_image([PageEvidence(url="https://example.com/empty", text="no tables here")]) is None


def test_format_evidence_context_carries_page_url_and_table():
    page = evidence_from_html("https://example.com/pricing", PRICING_HTML)
    block = format_evidence_context([page])
    assert "Source pages fetched just now" in block
    assert "https://example.com/pricing" in block
    assert "| Model | Input / 1M | Output / 1M |" in block
    assert format_evidence_context([]) == ""


@pytest.mark.asyncio
async def test_collect_page_evidence_fetches_only_public_pages(monkeypatch):
    async def fake_fetch(url, *, timeout_s=8.0):
        return PRICING_HTML if url.endswith("/pricing") else ""

    monkeypatch.setattr("chaosx_bot.web_sources.fetch_page_html", fake_fetch)
    results = [
        {"title": "private", "url": "http://127.0.0.1/secret", "snippet": ""},
        {"title": "pricing", "url": "https://example.com/pricing", "snippet": "prices"},
        {"title": "missing", "url": "https://example.com/none", "snippet": ""},
    ]
    pages = await collect_page_evidence(results, max_pages=2)
    assert [page.url for page in pages] == ["https://example.com/pricing"]


@pytest.mark.asyncio
async def test_web_grounder_search_evidence_returns_context_and_image(monkeypatch):
    async def fake_results(query):
        return [{"title": "Pricing", "url": "https://example.com/pricing", "snippet": "input $0.30"}]

    async def fake_collect(results, *, max_pages=2):
        return [evidence_from_html("https://example.com/pricing", PRICING_HTML)]

    monkeypatch.setattr("chaosx_bot.web_grounding.WebGrounder.search_results", staticmethod(fake_results))
    monkeypatch.setattr("chaosx_bot.web_grounding.collect_page_evidence", fake_collect)

    grounder = WebGrounder()
    context, image = await grounder.search_evidence("what does deepseek flash cost per million tokens?")
    assert "Web search results" in context
    assert "Source pages fetched just now" in context
    assert "https://example.com/pricing" in context
    assert image is not None and image.png[:4] == b"\x89PNG"


@pytest.mark.asyncio
async def test_web_grounder_search_evidence_disabled_or_empty_stays_plain(monkeypatch):
    async def fake_results(query):
        return [{"title": "Pricing", "url": "https://example.com/pricing", "snippet": "input $0.30"}]

    async def fake_collect(results, *, max_pages=2):  # pragma: no cover - must not run
        raise AssertionError("page fetching must stay off when evidence is disabled")

    monkeypatch.setattr("chaosx_bot.web_grounding.WebGrounder.search_results", staticmethod(fake_results))
    monkeypatch.setattr("chaosx_bot.web_grounding.collect_page_evidence", fake_collect)

    context, image = await WebGrounder().search_evidence("q", max_pages=0)
    assert "Web search results" in context
    assert "Source pages fetched just now" not in context
    assert image is None
