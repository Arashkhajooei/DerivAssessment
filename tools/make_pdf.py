#!/usr/bin/env python3
"""Render a Markdown document to PDF in the project's black-and-white theme.

Markdown -> HTML (python-markdown) -> PDF (headless Chrome). Chrome is used
rather than a Python PDF library because it gives real CSS support --
including locally installed fonts, page-break control, and repeating table
headers -- which the theme depends on.

Usage:
    python tools/make_pdf.py HANDBOOK.md            # -> HANDBOOK.pdf
    python tools/make_pdf.py HANDBOOK.md out.pdf
"""

from __future__ import annotations

import html as html_lib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Matches the deck: pure black and white, two greys for hierarchy, Google Sans.
CSS = """
@page {
  size: A4;
  margin: 18mm 16mm 20mm 16mm;
  @bottom-center { content: counter(page); }
}

:root {
  --ink:    #000000;
  --subtle: #6B6B6B;
  --hair:   #CFCFCF;
  --wash:   #F4F4F4;
}

* { box-sizing: border-box; }

body {
  font-family: "Google Sans", "Helvetica Neue", Arial, sans-serif;
  font-size: 9.6pt;
  line-height: 1.55;
  color: var(--ink);
  background: #fff;
  margin: 0;
  -webkit-font-smoothing: antialiased;
}

/* ---- headings ---------------------------------------------------------- */
h1 {
  font-size: 21pt;
  font-weight: 700;
  letter-spacing: -0.4px;
  margin: 0 0 14pt;
  padding-bottom: 8pt;
  border-bottom: 1.6px solid var(--ink);
  page-break-before: always;
  page-break-after: avoid;
}
h1:first-of-type { page-break-before: avoid; }

h2 {
  font-size: 14pt;
  font-weight: 700;
  letter-spacing: -0.2px;
  margin: 20pt 0 8pt;
  page-break-after: avoid;
}

h3 {
  font-size: 11pt;
  font-weight: 700;
  margin: 15pt 0 6pt;
  page-break-after: avoid;
}

h4 {
  font-size: 9.6pt;
  font-weight: 700;
  margin: 12pt 0 5pt;
  page-break-after: avoid;
}

p { margin: 0 0 8pt; orphans: 3; widows: 3; }

/* ---- lists ------------------------------------------------------------- */
ul, ol { margin: 0 0 9pt; padding-left: 16pt; }
li { margin-bottom: 3.5pt; }
li > ul, li > ol { margin-top: 3.5pt; }

/* ---- inline ------------------------------------------------------------ */
strong { font-weight: 700; }
em { font-style: italic; }
a { color: var(--ink); text-decoration: none; border-bottom: 0.5px solid var(--hair); }

code {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 8.4pt;
  background: var(--wash);
  padding: 1px 3.5px;
  border-radius: 2px;
}

/* ---- code blocks ------------------------------------------------------- */
pre {
  background: var(--wash);
  border: 0.5px solid var(--hair);
  border-radius: 3px;
  padding: 8pt 10pt;
  margin: 0 0 10pt;
  overflow-x: auto;
  page-break-inside: avoid;
}
pre code {
  background: none;
  padding: 0;
  font-size: 7.8pt;
  line-height: 1.45;
  white-space: pre;
}

/* ---- tables ------------------------------------------------------------ */
table {
  width: 100%;
  border-collapse: collapse;
  margin: 0 0 11pt;
  font-size: 8.6pt;
  page-break-inside: avoid;
}
thead { display: table-header-group; }   /* repeat header across page breaks */
th {
  text-align: left;
  font-weight: 700;
  font-size: 7.4pt;
  letter-spacing: 0.6px;
  text-transform: uppercase;
  color: var(--subtle);
  border-bottom: 1.2px solid var(--ink);
  padding: 4pt 7pt 4pt 0;
  vertical-align: bottom;
}
td {
  border-bottom: 0.5px solid var(--hair);
  padding: 5pt 7pt 5pt 0;
  vertical-align: top;
}
tr:last-child td { border-bottom: none; }
td:last-child, th:last-child { padding-right: 0; }

/* ---- blockquote -------------------------------------------------------- */
blockquote {
  margin: 0 0 10pt;
  padding: 7pt 0 7pt 12pt;
  border-left: 2.5px solid var(--ink);
  page-break-inside: avoid;
}
blockquote p { margin: 0 0 5pt; }
blockquote p:last-child { margin-bottom: 0; }

hr {
  border: none;
  border-top: 0.5px solid var(--hair);
  margin: 16pt 0;
}

/* ---- title block ------------------------------------------------------- */
.cover { page-break-after: always; padding-top: 42mm; }
.cover .eyebrow {
  font-size: 8pt; font-weight: 700; letter-spacing: 3px;
  color: var(--subtle); text-transform: uppercase; margin-bottom: 10mm;
}
.cover h1 {
  font-size: 34pt; line-height: 1.08; letter-spacing: -1px;
  border: none; padding: 0; margin: 0 0 6mm; page-break-before: avoid;
}
.cover .sub { font-size: 12pt; color: var(--subtle); max-width: 118mm; margin-bottom: 22mm; }
.cover .meta { font-size: 8.6pt; color: var(--subtle); border-top: 0.5px solid var(--hair); padding-top: 5mm; }
.cover .meta strong { color: var(--ink); }
"""

COVER = """
<div class="cover">
  <div class="eyebrow">Technical Assessment</div>
  <h1>{title}</h1>
  <div class="sub">{subtitle}</div>
  <div class="meta"><strong>Arash Khajooei</strong> &nbsp;·&nbsp; Deriv &nbsp;·&nbsp; AI Engineer</div>
</div>
"""


def build_html(md_path: Path, title: str, subtitle: str) -> str:
    text = md_path.read_text(encoding="utf-8")

    # The first H1 and the intro become the cover; drop them from the body.
    text = re.sub(r"\A# .+?\n", "", text, count=1)

    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list", "toc"],
    )
    cover = COVER.format(title=html_lib.escape(title), subtitle=html_lib.escape(subtitle))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html_lib.escape(title)}</title><style>{CSS}</style></head>"
        f"<body>{cover}{body}</body></html>"
    )


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    md_path = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else md_path.with_suffix(".pdf")

    titles = {
        "HANDBOOK.md": ("Handbook", "The complete walkthrough: what was asked, what was built, and why."),
        "TASK.md": ("Task Brief", "The assessment brief, with a requirement-by-requirement coverage map."),
        "README.md": ("README", "Setup, artifacts, and how to run the harness."),
    }
    title, subtitle = titles.get(md_path.name, (md_path.stem, ""))

    html = build_html(md_path, title, subtitle)

    with tempfile.TemporaryDirectory() as tmp:
        html_file = Path(tmp) / "doc.html"
        html_file.write_text(html, encoding="utf-8")
        subprocess.run(
            [
                CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                f"--print-to-pdf={out.resolve()}", html_file.resolve().as_uri(),
            ],
            check=True, capture_output=True, timeout=180,
        )

    print(f"wrote {out}  ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
