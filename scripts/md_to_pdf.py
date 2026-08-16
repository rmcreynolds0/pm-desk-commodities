#!/usr/bin/env python3
"""
md_to_pdf.py — render project markdown documents to PDF.

PIPELINE: markdown -> styled HTML -> Chromium (Playwright) -> PDF

Why this route rather than pandoc's usual one: pandoc needs a LaTeX engine,
and LaTeX chokes on the Unicode these documents use (★ ✓ ✗ → ≥ ±, emoji).
Chromium renders all of it natively, along with the many tables, and produces
consistent output without installing a TeX distribution.

Usage:
    python scripts/md_to_pdf.py                    # render the three deliverables
    python scripts/md_to_pdf.py docs/OTHER.md      # render specific files
"""
from __future__ import annotations

import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "pdf"

# The three deliverables, in order. `compact` applies a denser layout — used for
# the hiring one-pager, which is specified as 1-2 pages and otherwise spills a
# small amount onto a third. Tightening layout is preferable to cutting content
# that earns its place.
DEFAULT_DOCS = [
    (ROOT / "docs" / "01_TECHNICAL_INFRASTRUCTURE.md", False),
    (ROOT / "docs" / "02_PROJECT_INTRO_HIRING.md", True),
    (ROOT / "docs" / "03_SUMMER_RESULTS.md", False),
]

# Denser typography for short documents that must hit a page budget.
COMPACT_CSS = """
@page { margin: 12mm 13mm 13mm 13mm; }
body { font-size: 9.1pt; line-height: 1.34; }
h1 { font-size: 16.5pt; margin-bottom: .3em; }
h2 { font-size: 12pt; margin: .95em 0 .32em; }
h3 { font-size: 10.3pt; margin: .7em 0 .25em; }
table { font-size: 8.3pt; margin: .5em 0; }
th, td { padding: 3px 5px; }
hr { margin: .9em 0; }
p { margin: .45em 0; }
ul, ol { margin: .4em 0; }
li { margin: .1em 0; }
"""

# Print stylesheet. Tuned for documents that are mostly prose + dense tables:
# generous table padding, avoid splitting rows across pages, and a monospace
# stack that renders the box-drawing characters in the architecture diagrams.
CSS = """
@page { size: Letter; margin: 18mm 16mm 20mm 16mm; }
body {
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 10.2pt; line-height: 1.5; color: #1a1a1a; max-width: 100%;
}
h1 { font-size: 20pt; margin: 0 0 .5em; padding-bottom: .25em;
     border-bottom: 2.5px solid #2c3e50; color: #1a2733; }
h2 { font-size: 14pt; margin: 1.6em 0 .5em; padding-bottom: .18em;
     border-bottom: 1px solid #ccd4db; color: #22303c; page-break-after: avoid; }
h3 { font-size: 11.6pt; margin: 1.2em 0 .4em; color: #2c3e50;
     page-break-after: avoid; }
h4 { font-size: 10.6pt; margin: 1em 0 .3em; color: #37474f; }
p, li { orphans: 3; widows: 3; }
code { font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace;
       font-size: 8.8pt; background: #f4f6f8; padding: 1px 4px;
       border-radius: 3px; }
pre { background: #f7f9fa; border: 1px solid #e1e6ea; border-left: 3px solid #4c8bf5;
      padding: 9px 11px; border-radius: 4px; overflow-x: auto;
      page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 8.2pt; line-height: 1.35; }
table { border-collapse: collapse; width: 100%; margin: .8em 0;
        font-size: 9.1pt; page-break-inside: auto; }
th { background: #eef2f5; border: 1px solid #c9d2d9; padding: 5px 7px;
     text-align: left; font-weight: 600; }
td { border: 1px solid #d8dfe4; padding: 4px 7px; vertical-align: top; }
tr { page-break-inside: avoid; page-break-after: auto; }
tr:nth-child(even) td { background: #fafbfc; }
blockquote { border-left: 3px solid #f5a623; background: #fffdf6;
             margin: .9em 0; padding: .5em .9em; color: #4a4a4a; }
blockquote p { margin: .3em 0; }
hr { border: none; border-top: 1px solid #dde3e8; margin: 1.6em 0; }
a { color: #1a5fb4; text-decoration: none; }
strong { color: #111; }
ul, ol { padding-left: 1.4em; }
li { margin: .18em 0; }
"""

HTML_SHELL = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>{css}</style></head><body>{body}</body></html>"""


def render(md_path: Path, out_path: Path, compact: bool = False) -> None:
    text = md_path.read_text(encoding="utf-8")
    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
    )
    css = CSS + (COMPACT_CSS if compact else "")
    html = HTML_SHELL.format(title=md_path.stem, css=css, body=body)

    tmp_html = out_path.with_suffix(".html")
    tmp_html.write_text(html, encoding="utf-8")

    m = "12mm" if compact else "18mm"
    side = "13mm" if compact else "16mm"

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(tmp_html.resolve().as_uri(), wait_until="networkidle")
        page.pdf(
            path=str(out_path),
            format="Letter",
            print_background=True,
            margin={"top": m, "bottom": ("14mm" if compact else "20mm"),
                    "left": side, "right": side},
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=(
                '<div style="font-size:7.5pt;color:#8a9199;width:100%;'
                f'padding:0 {side};display:flex;justify-content:space-between;">'
                f'<span>{md_path.stem}</span>'
                '<span class="pageNumber"></span></div>'),
        )
        browser.close()
    tmp_html.unlink(missing_ok=True)      # keep only the PDF


def main() -> int:
    if sys.argv[1:]:
        docs = [((Path(a) if Path(a).is_absolute() else ROOT / a), False)
                for a in sys.argv[1:]]
    else:
        docs = DEFAULT_DOCS
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for md, compact in docs:
        if not md.exists():
            print(f"[skip] {md} does not exist")
            continue
        out = OUT_DIR / (md.stem + ".pdf")
        render(md, out, compact=compact)
        kb = out.stat().st_size / 1024
        print(f"  {md.name:<38} -> {out.relative_to(ROOT)}  ({kb:,.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
