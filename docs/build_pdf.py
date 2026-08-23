"""Render a document in this folder to a typeset PDF.

    .venv\\Scripts\\python.exe docs\\build_pdf.py architecture.md

The markdown beside it is the source of truth and lives in git; the PDF is the copy that
gets handed to someone. Regenerate rather than editing the PDF, so the two cannot drift.

The subset of markdown understood here is the subset these documents use: headings,
paragraphs, bullets, pipe tables, fenced code blocks and rules. Anything else is rendered
as a paragraph rather than silently dropped.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

DOCS = Path(__file__).resolve().parent

INK = colors.HexColor("#12212e")
BODY = colors.HexColor("#26323d")
MUTED = colors.HexColor("#5d6b78")
ACCENT = colors.HexColor("#1c4f82")
RULE = colors.HexColor("#c9d4de")
BAND = colors.HexColor("#eef3f7")

TITLE = "Gambling Host Tracker"
SUBTITLE = "Architecture and Technology"


# --------------------------------------------------------------------------- styles
def build_styles() -> dict:
    base = getSampleStyleSheet()
    s = {}
    s["body"] = ParagraphStyle(
        "body", parent=base["Normal"], fontName="Helvetica", fontSize=9.5, leading=14.5,
        textColor=BODY, alignment=TA_JUSTIFY, spaceAfter=7,
    )
    s["h1"] = ParagraphStyle(
        "h1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=17, leading=21,
        textColor=INK, spaceBefore=6, spaceAfter=10, keepWithNext=1,
    )
    s["h2"] = ParagraphStyle(
        "h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=12.5, leading=16,
        textColor=ACCENT, spaceBefore=14, spaceAfter=6, keepWithNext=1,
    )
    s["h3"] = ParagraphStyle(
        "h3", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=10.5, leading=14,
        textColor=INK, spaceBefore=11, spaceAfter=4, keepWithNext=1,
    )
    s["bullet"] = ParagraphStyle(
        "bullet", parent=s["body"], leftIndent=11, bulletIndent=2, spaceAfter=4,
        alignment=0,
    )
    s["cell"] = ParagraphStyle(
        "cell", parent=base["Normal"], fontName="Helvetica", fontSize=8.4, leading=11.6,
        textColor=BODY,
    )
    s["cellhead"] = ParagraphStyle(
        "cellhead", parent=s["cell"], fontName="Helvetica-Bold", textColor=colors.white,
    )
    s["code"] = ParagraphStyle(
        "code", parent=base["Normal"], fontName="Courier", fontSize=7.8, leading=10.6,
        textColor=INK, leftIndent=6, rightIndent=6, spaceBefore=2, spaceAfter=2,
    )
    s["cover_title"] = ParagraphStyle(
        "cover_title", parent=base["Title"], fontName="Helvetica-Bold", fontSize=30,
        leading=35, textColor=INK, alignment=0, spaceAfter=4,
    )
    s["cover_sub"] = ParagraphStyle(
        "cover_sub", parent=base["Normal"], fontName="Helvetica", fontSize=15, leading=20,
        textColor=ACCENT, alignment=0,
    )
    s["cover_note"] = ParagraphStyle(
        "cover_note", parent=base["Normal"], fontName="Helvetica", fontSize=9.5, leading=15,
        textColor=MUTED, alignment=0,
    )
    s["toc1"] = ParagraphStyle(
        "toc1", fontName="Helvetica-Bold", fontSize=9.5, leading=14, textColor=INK,
    )
    s["toc2"] = ParagraphStyle(
        "toc2", fontName="Helvetica", fontSize=8.5, leading=12, textColor=BODY, leftIndent=14,
    )
    s["caption"] = ParagraphStyle(
        "caption", parent=base["Normal"], fontName="Helvetica-Oblique", fontSize=8,
        leading=11, textColor=MUTED, alignment=TA_CENTER, spaceBefore=3,
    )
    return s


# --------------------------------------------------------------------------- inline
def inline(text: str) -> str:
    """Markdown emphasis and code spans to ReportLab's inline markup."""
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out = re.sub(r"`([^`]+)`", r'<font face="Courier" size="8.4">\1</font>', out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", out)
    out = out.replace("—", "&#8212;").replace("–", "&#8211;")
    return out


# --------------------------------------------------------------------------- parsing
def split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_divider(line: str) -> bool:
    return bool(re.fullmatch(r"\|?[\s:\-|]+\|?", line.strip())) and "-" in line


def parse(md: str) -> list[tuple]:
    """The document as a flat list of (kind, payload) blocks."""
    blocks: list[tuple] = []
    lines = md.split("\n")
    i = 0
    para: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        nonlocal para, bullets
        if para:
            blocks.append(("p", " ".join(para)))
            para = []
        if bullets:
            blocks.append(("ul", bullets))
            bullets = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            blocks.append(("code", code))
            i += 1
            continue

        if not stripped:
            flush()
            i += 1
            continue

        if stripped == "---":
            flush()
            blocks.append(("rule", None))
            i += 1
            continue

        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            flush()
            blocks.append((f"h{len(heading.group(1))}", heading.group(2)))
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and is_divider(lines[i + 1]):
            flush()
            header = split_row(stripped)
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            blocks.append(("table", (header, rows)))
            continue

        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet:
            if para:
                blocks.append(("p", " ".join(para)))
                para = []
            bullets.append(bullet.group(1))
            i += 1
            continue

        if bullets and line.startswith(("  ", "\t")):
            bullets[-1] += " " + stripped
            i += 1
            continue

        if bullets:
            flush()
        para.append(stripped)
        i += 1

    flush()
    return blocks


# --------------------------------------------------------------------------- flowables
def make_table(header: list[str], rows: list[list[str]], styles: dict, width: float) -> Table:
    blank_header = not any(h for h in header)
    data = []
    if not blank_header:
        data.append([Paragraph(inline(c), styles["cellhead"]) for c in header])
    for row in rows:
        row = row + [""] * (len(header) - len(row))
        data.append([Paragraph(inline(c), styles["cell"]) for c in row[: len(header)]])

    columns = len(header)
    if columns == 2:
        widths = [width * 0.32, width * 0.68]
    elif columns == 3:
        widths = [width * 0.20, width * 0.29, width * 0.51]
    else:
        widths = [width / columns] * columns

    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if blank_header:
        style.append(("BACKGROUND", (0, 0), (0, -1), BAND))
    else:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, ACCENT),
        ]
        for r in range(2, len(data), 2):
            style.append(("BACKGROUND", (0, r), (-1, r), BAND))

    table = Table(data, colWidths=widths, repeatRows=0 if blank_header else 1, hAlign="LEFT")
    table.setStyle(TableStyle(style))
    return table


def make_code(lines: list[str], styles: dict, width: float) -> Table:
    text = "<br/>".join(
        line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace(" ", "&nbsp;")
        or "&nbsp;"
        for line in lines
    )
    table = Table([[Paragraph(text, styles["code"])]], colWidths=[width], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), BAND),
                ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return table


def rule_flowable(width: float) -> Table:
    table = Table([[""]], colWidths=[width], rowHeights=[1], hAlign="LEFT")
    table.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.6, RULE)]))
    return table


# --------------------------------------------------------------------------- document
class Document(BaseDocTemplate):
    """Cover page, then numbered body pages with a running header."""

    def __init__(self, path: Path, **kw):
        super().__init__(str(path), pagesize=A4, **kw)
        margin = 20 * mm
        frame = Frame(
            margin, 22 * mm, A4[0] - 2 * margin, A4[1] - 22 * mm - 24 * mm, id="body"
        )
        cover = Frame(margin, margin, A4[0] - 2 * margin, A4[1] - 2 * margin, id="cover")
        self.addPageTemplates(
            [
                PageTemplate(id="cover", frames=[cover], onPage=self.paint_cover),
                PageTemplate(id="body", frames=[frame], onPageEnd=self.paint_body),
            ]
        )
        self.section = ""

    def paint_cover(self, canvas, doc) -> None:
        # multiBuild runs the story more than once to resolve the contents; without this
        # the header on the early pages of pass two still carries the last section of
        # pass one.
        self.section = ""
        canvas.saveState()
        canvas.setFillColor(ACCENT)
        canvas.rect(0, A4[1] - 14 * mm, A4[0], 14 * mm, stroke=0, fill=1)
        canvas.setFillColor(INK)
        canvas.rect(20 * mm, 96 * mm, 34 * mm, 1.6, stroke=0, fill=1)
        canvas.restoreState()

    def paint_body(self, canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, A4[1] - 14 * mm, f"{TITLE} — {SUBTITLE}")
        if self.section:
            canvas.drawRightString(A4[0] - 20 * mm, A4[1] - 14 * mm, self.section)
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(20 * mm, A4[1] - 16 * mm, A4[0] - 20 * mm, A4[1] - 16 * mm)
        canvas.line(20 * mm, 16 * mm, A4[0] - 20 * mm, 16 * mm)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(20 * mm, 11.5 * mm, "Internal — AML systems documentation")
        canvas.drawRightString(A4[0] - 20 * mm, 11.5 * mm, f"Page {canvas.getPageNumber() - 1}")
        canvas.restoreState()

    def afterFlowable(self, flowable) -> None:
        """Feed headings to the table of contents, and to the running header."""
        if not isinstance(flowable, Paragraph):
            return
        style = flowable.style.name
        if style == "h2":
            text = flowable.getPlainText()
            self.section = text
            self.notify("TOCEntry", (0, text, self.page - 1))
        elif style == "h3":
            self.notify("TOCEntry", (1, flowable.getPlainText(), self.page - 1))


def cover(styles: dict, width: float) -> list:
    # Dhaka time, like every date this system prints.
    generated = datetime.now(timezone(timedelta(hours=6))).strftime("%d %B %Y")
    return [
        Spacer(1, 62 * mm),
        Paragraph(TITLE, styles["cover_title"]),
        Paragraph(SUBTITLE, styles["cover_sub"]),
        Spacer(1, 26 * mm),
        Paragraph(
            "A collection system for the mobile-wallet and bank account numbers that "
            "gambling sites publish for deposits, built so that an AML team can blocklist "
            "them and evidence the decision.",
            styles["cover_note"],
        ),
        Spacer(1, 14 * mm),
        Paragraph(
            f"<b>Document</b>&nbsp;&nbsp;Architecture and technology<br/>"
            f"<b>Status</b>&nbsp;&nbsp;Current as built<br/>"
            f"<b>Generated</b>&nbsp;&nbsp;{generated}<br/>"
            f"<b>Source</b>&nbsp;&nbsp;docs/architecture.md",
            styles["cover_note"],
        ),
    ]


def build(source: Path, target: Path) -> None:
    styles = build_styles()
    doc = Document(target, title=f"{TITLE} — {SUBTITLE}", author="Gambling Host Tracker")
    width = doc.width

    story: list = []
    story += cover(styles, width)
    story.append(NextPageTemplate("body"))
    story.append(PageBreak())

    toc = TableOfContents()
    toc.levelStyles = [styles["toc1"], styles["toc2"]]
    # No dot leaders: at this many entries they read as noise rather than as guidance.
    toc.dotsMinLevel = 99
    story.append(Paragraph("Contents", styles["h1"]))
    story.append(Spacer(1, 4))
    story.append(toc)
    story.append(PageBreak())

    seen_title = False
    blocks = parse(source.read_text(encoding="utf-8"))
    for index, (kind, payload) in enumerate(blocks):
        # A heading whose whole section is one diagram belongs on the diagram's page.
        # keepWithNext cannot see past the wrapper the diagram travels in.
        if kind in ("h2", "h3") and index + 1 < len(blocks) and blocks[index + 1][0] == "code":
            story.append(
                KeepTogether(
                    [
                        Paragraph(inline(payload), styles[kind]),
                        make_code(blocks[index + 1][1], styles, width),
                        Spacer(1, 8),
                    ]
                )
            )
            blocks[index + 1] = ("done", None)
            continue
        if kind == "h1":
            # The document's own title and subtitle are already on the cover.
            if not seen_title:
                seen_title = True
                continue
            story.append(Paragraph(inline(payload), styles["h1"]))
        elif kind == "h2":
            # The subtitle is the cover's, not a section of the document.
            if payload.strip() == SUBTITLE:
                continue
            story.append(Paragraph(inline(payload), styles["h2"]))
        elif kind in ("h3", "h4"):
            story.append(Paragraph(inline(payload), styles["h3"]))
        elif kind == "p":
            if payload.startswith("## "):
                continue
            story.append(Paragraph(inline(payload), styles["body"]))
        elif kind == "ul":
            for item in payload:
                story.append(Paragraph(inline(item), styles["bullet"], bulletText="\u2022"))
            story.append(Spacer(1, 5))
        elif kind == "table":
            header, rows = payload
            story.append(Spacer(1, 3))
            story.append(make_table(header, rows, styles, width))
            story.append(Spacer(1, 9))
        elif kind == "code":
            story.append(KeepTogether([make_code(payload, styles, width), Spacer(1, 8)]))
        elif kind == "done":
            continue
        elif kind == "rule":
            story.append(Spacer(1, 3))

    doc.multiBuild(story)


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "architecture.md"
    source = (DOCS / name).resolve()
    if not source.exists():
        print(f"no such document: {source}")
        return 1
    target = source.with_suffix(".pdf")
    build(source, target)
    print(f"{target}  ({target.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
