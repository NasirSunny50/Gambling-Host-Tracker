"""The payee list as a PDF a person can file, sign off, or hand to a regulator.

The CSV is for a system to read; this is for a person. That difference decides everything
here: it is paginated, every page states what it is and where it came from, and each page
carries its own page number and the digest-bearing footer, because a printed page that has
been separated from its cover sheet must still say what it belongs to.

Nothing about the data is reformatted for looks — the numbers are the numbers. What the
report adds is provenance: which filters produced this set, when, from which machine, and
how many rows there were in total.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

# The product logo, shipped with the api package; the report shares it with the portal so
# the two cannot drift apart.
LOGO_PATH = Path(__file__).resolve().parents[1] / "api" / "assets" / "logo-badge.png"

# Bangladesh keeps one offset all year, so it needs no tz database to be right.
DHAKA = timezone(timedelta(hours=6))

TITLE = "Payee report"
SUBTITLE = "Payment accounts advertised for deposits"

# Deliberately muted: this is a document to be read and photocopied, not a dashboard.
INK = (0.11, 0.13, 0.16)
QUIET = (0.42, 0.46, 0.51)
RULE = (0.80, 0.83, 0.87)
BAND = (0.945, 0.955, 0.965)



# A payee name can be Bengali - the site publishes them that way, and one of these reports
# may be the only record of it. The built-in Helvetica cannot draw those letters: it does
# not fail, it draws the wrong glyphs, which is the worst outcome for a document meant to
# be evidence. So a Unicode face is used when the machine has one.
UNICODE_FONT_CANDIDATES = (
    ("Nirmala", "C:/Windows/Fonts/Nirmala.ttf"),
    ("NotoSansBengali", "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf"),
    ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)


def _register_body_font() -> str:
    """Return the font to set text in: a Unicode face if one is installed."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for name, path in UNICODE_FONT_CANDIDATES:
        if not Path(path).is_file():
            continue
        try:
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, path))
            return name
        except Exception:  # noqa: BLE001, S112 - an unreadable font is not our font
            continue
    return "Helvetica"


def _latin_safe(text: str, font: str) -> str:
    """Keep a name honest when the only font available cannot draw it.

    Helvetica maps unknown code points onto whatever glyph sits at that byte, so a Bengali
    name comes out as confident nonsense. A run of question marks is worse to look at and
    far better to trust.
    """
    if font != "Helvetica":
        return text
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        return "".join(ch if ord(ch) < 256 else "?" for ch in text)
    return text


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    width: float
    align: str = "left"


COLUMNS = (
    Column("channel", "Channel", 68),
    Column("number", "Account number", 118),
    Column("name", "Name", 132),
    Column("bank", "Bank", 96),
    Column("site", "Site", 74),
    Column("times", "Seen", 34, align="right"),
    Column("last_seen", "Last seen", 92),
)


def _stamp(value) -> str:
    if value is None:
        return "—"
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(DHAKA).strftime("%d/%m/%Y %I:%M %p")


def _clip(canvas, text: str, width: float, font: str, size: float) -> str:
    """Trim to fit, with an ellipsis, so a long name cannot run into the next column."""
    text = str(text or "")
    if canvas.stringWidth(text, font, size) <= width:
        return text
    while text and canvas.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


class _Report:
    """Draws the document. One instance per file, so page numbering stays honest."""

    PAGE_MARGIN = 34
    HEADER_HEIGHT = 62
    FOOTER_HEIGHT = 34
    ROW_HEIGHT = 17
    HEAD_SIZE = 7.5
    BODY_SIZE = 8

    def __init__(self, canvas, page_size, meta: dict, body_font: str = "Helvetica"):
        self.c = canvas
        self.width, self.height = page_size
        self.meta = meta
        self.page = 0
        self.y = 0.0
        self.body_font = body_font

    # ------------------------------------------------------------------ furniture

    def header(self) -> None:
        c, w, h = self.c, self.width, self.height
        left, right = self.PAGE_MARGIN, w - self.PAGE_MARGIN

        # The product logo, the same badge the portal shows. Drawn from the shipped PNG so
        # the report and the portal cannot drift apart; if the asset is somehow missing the
        # header simply carries no mark rather than failing the export.
        if LOGO_PATH.exists():
            c.drawImage(
                str(LOGO_PATH), left, h - 46, 22, 22, mask="auto", preserveAspectRatio=True
            )

        c.setFillColorRGB(*INK)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(left + 30, h - 32, TITLE)
        c.setFillColorRGB(*QUIET)
        c.setFont("Helvetica", 7.5)
        c.drawString(left + 30, h - 42, SUBTITLE)

        c.setFont("Helvetica", 7.5)
        c.drawRightString(right, h - 32, self.meta["generated"])
        c.drawRightString(right, h - 42, self.meta["scope"])

        c.setStrokeColorRGB(*RULE)
        c.setLineWidth(0.6)
        c.line(left, h - 54, right, h - 54)

    def footer(self) -> None:
        c, w = self.c, self.width
        left, right = self.PAGE_MARGIN, w - self.PAGE_MARGIN
        y = self.FOOTER_HEIGHT

        c.setStrokeColorRGB(*RULE)
        c.setLineWidth(0.6)
        c.line(left, y + 12, right, y + 12)

        c.setFillColorRGB(*QUIET)
        c.setFont("Helvetica", 6.8)
        c.drawString(left, y, self.meta["footer"])
        # Every page says which it is, because pages get separated from their cover sheet.
        c.drawRightString(right, y, f"Page {self.page}")

    def new_page(self) -> None:
        if self.page:
            self.footer()
            self.c.showPage()
        self.page += 1
        self.header()
        self.y = self.height - self.HEADER_HEIGHT - 18
        self.column_heads()

    def column_heads(self) -> None:
        c = self.c
        x = self.PAGE_MARGIN
        c.setFillColorRGB(*BAND)
        c.rect(self.PAGE_MARGIN, self.y - 5, self.width - 2 * self.PAGE_MARGIN, 15, stroke=0, fill=1)
        c.setFillColorRGB(*QUIET)
        c.setFont("Helvetica-Bold", self.HEAD_SIZE)
        for col in COLUMNS:
            if col.align == "right":
                c.drawRightString(x + col.width - 6, self.y, col.label.upper())
            else:
                c.drawString(x, self.y, col.label.upper())
            x += col.width
        self.y -= self.ROW_HEIGHT

    # ------------------------------------------------------------------ the rows

    def row(self, values: dict, shaded: bool) -> None:
        if self.y < self.FOOTER_HEIGHT + 26:
            self.new_page()

        c = self.c
        if shaded:
            c.setFillColorRGB(*BAND)
            c.rect(self.PAGE_MARGIN, self.y - 4.5, self.width - 2 * self.PAGE_MARGIN,
                   self.ROW_HEIGHT - 2, stroke=0, fill=1)

        x = self.PAGE_MARGIN
        for col in COLUMNS:
            raw = values.get(col.key)
            # Digits and timestamps line up in a monospace face; names need the Unicode one.
            font = "Courier" if col.key in ("number", "last_seen") else self.body_font
            c.setFillColorRGB(*(QUIET if raw in (None, "", "—") else INK))
            shown = _latin_safe(str(raw), font) if raw not in (None, "") else "—"
            text = _clip(c, shown, col.width - 8, font, self.BODY_SIZE)
            c.setFont(font, self.BODY_SIZE)
            if col.align == "right":
                c.drawRightString(x + col.width - 6, self.y, text)
            else:
                c.drawString(x, self.y, text)
            x += col.width

        c.setStrokeColorRGB(*RULE)
        c.setLineWidth(0.25)
        c.line(self.PAGE_MARGIN, self.y - 5, self.width - self.PAGE_MARGIN, self.y - 5)
        self.y -= self.ROW_HEIGHT

    def total(self, count: int) -> None:
        self.y -= 6
        if self.y < self.FOOTER_HEIGHT + 26:
            self.new_page()
        self.c.setFillColorRGB(*INK)
        self.c.setFont("Helvetica-Bold", 8)
        self.c.drawString(self.PAGE_MARGIN, self.y, f"{count} payees in this report")


def build_pdf(rows: list[dict], *, scope: str, actor: str, channel_labels: dict) -> bytes:
    """Render the payee rows as a PDF and return its bytes.

    ``rows`` are the same records the Payees table shows. ``scope`` describes the filters
    that produced them, so the document says what it is a report *of* rather than leaving
    the reader to guess whether it is everything.
    """
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas as pdfcanvas

    page_size = landscape(A4)
    buffer = BytesIO()
    c = pdfcanvas.Canvas(buffer, pagesize=page_size)

    now = datetime.now(DHAKA)
    c.setTitle(f"{TITLE} — {now:%d/%m/%Y}")
    c.setAuthor("Gambling Host Tracker")
    c.setSubject(SUBTITLE)

    meta = {
        "generated": f"Generated {now:%d/%m/%Y %I:%M %p}",
        "scope": scope,
        "footer": (
            f"Gambling Host Tracker · exported by {actor} · "
            "collected evidence retained under the organisation's PII and AML policy"
        ),
    }

    report = _Report(c, page_size, meta, body_font=_register_body_font())
    report.new_page()
    for index, row in enumerate(rows):
        report.row(
            {
                "channel": channel_labels.get(row.get("channel"), row.get("channel")),
                "number": row.get("number") or "—",
                "name": row.get("name") or "—",
                "bank": row.get("bank") or "—",
                "site": row.get("site") or "—",
                "times": row.get("times"),
                "last_seen": _stamp(row.get("last_seen")),
            },
            shaded=index % 2 == 1,
        )
    report.total(len(rows))
    report.footer()
    c.showPage()
    c.save()
    return buffer.getvalue()
