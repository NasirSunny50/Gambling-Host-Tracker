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


# Space between one column's text and the next column's edge. Every width below includes
# it, so widening a column widens the gap with it and the table cannot go back to looking
# packed. Without it the columns sat flush and the page read as one block of text.
GUTTER = 12

# Landscape A4 leaves 774pt between the margins. These add to 772, so the table uses the
# width it has: two columns were being truncated while a sixth of the page stayed blank.
# Bank and Last seen are sized to hold their longest real value uncut - a full bank name
# ("Dutch-Bangla Bank Limited") and a full timestamp with the date, time and meridiem -
# because a bank abbreviated by the page and a bank abbreviated by the site look the same
# to a reader, and a truncated timestamp is not evidence of anything.
COLUMNS = (
    Column("channel", "Channel", 78),
    Column("number", "Account number", 132),
    Column("name", "Name", 152),
    Column("bank", "Bank", 152),
    Column("site", "Site", 82),
    # Left, like every other column here and every column in the portal's tables. A single
    # right-aligned count between two left-aligned columns left a ragged gap in the middle
    # of the row, which is exactly where the eye needs a straight edge to follow.
    Column("times", "Seen", 40),
    Column("last_seen", "Last seen", 136),
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
    HEADER_HEIGHT = 80
    FOOTER_HEIGHT = 34
    # A row of text needs air above and below it as much as it needs it on either side.
    # At 17pt the lines nearly touched, which is what made the table read as congested
    # even where nothing was actually truncated.
    ROW_HEIGHT = 22
    HEAD_SIZE = 8.5
    # This is printed and photocopied, and 8pt does not survive either. 9.5 is the
    # smallest size that still holds every column on one landscape sheet.
    BODY_SIZE = 9.5

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
                str(LOGO_PATH), left, h - 48, 26, 26, mask="auto", preserveAspectRatio=True
            )

        c.setFillColorRGB(*INK)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(left + 34, h - 34, TITLE)
        c.setFillColorRGB(*QUIET)
        c.setFont("Helvetica", 8.5)
        c.drawString(left + 34, h - 45, SUBTITLE)

        c.setFont("Helvetica", 8.5)
        c.drawRightString(right, h - 34, self.meta["generated"])
        c.drawRightString(right, h - 45, self.meta["count"])

        # What this is a report *of*, on its own line and in the reading ink rather than
        # the grey used for furniture. It was a small grey note beside the timestamp, which
        # is where a reader's eye goes last - and a filtered report mistaken for the whole
        # picture is the one way this document can actively mislead someone.
        c.setFillColorRGB(*INK)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(left, h - 63, "Filters")
        c.setFont("Helvetica", 9)
        c.drawString(left + 42, h - 63, self.meta["scope"])

        c.setStrokeColorRGB(*RULE)
        c.setLineWidth(0.6)
        c.line(left, h - 72, right, h - 72)

    def footer(self) -> None:
        """The product and which page this is. Nothing else.

        It used to carry a sentence about the retention policy on every page, and the IP
        the export was requested from. A line repeated on every sheet stops being read on
        the second sheet, and a loopback address identifies nobody - the access log keeps
        the real record of who exported what, where it can be queried.
        """
        c, w = self.c, self.width
        left, right = self.PAGE_MARGIN, w - self.PAGE_MARGIN
        y = self.FOOTER_HEIGHT

        c.setStrokeColorRGB(*RULE)
        c.setLineWidth(0.6)
        c.line(left, y + 13, right, y + 13)

        c.setFillColorRGB(*QUIET)
        c.setFont("Helvetica", 8)
        c.drawString(left, y, "Gambling Host Tracker")
        # Every page says which it is, because pages get separated from their cover sheet.
        c.drawRightString(right, y, f"Page {self.page}")

    def new_page(self) -> None:
        if self.page:
            self.footer()
            self.c.showPage()
        self.page += 1
        self.header()
        self.y = self.height - self.HEADER_HEIGHT - 20
        self.column_heads()

    def column_heads(self) -> None:
        c = self.c
        x = self.PAGE_MARGIN
        c.setFillColorRGB(*BAND)
        c.rect(self.PAGE_MARGIN, self.y - 6, self.width - 2 * self.PAGE_MARGIN, 18, stroke=0, fill=1)
        c.setFillColorRGB(*QUIET)
        c.setFont("Helvetica-Bold", self.HEAD_SIZE)
        for col in COLUMNS:
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
            c.rect(self.PAGE_MARGIN, self.y - 6, self.width - 2 * self.PAGE_MARGIN,
                   self.ROW_HEIGHT - 2, stroke=0, fill=1)

        x = self.PAGE_MARGIN
        for col in COLUMNS:
            raw = values.get(col.key)
            # Digits and timestamps line up in a monospace face; names need the Unicode one.
            font = "Courier" if col.key in ("number", "last_seen") else self.body_font
            c.setFillColorRGB(*(QUIET if raw in (None, "", "—") else INK))
            shown = _latin_safe(str(raw), font) if raw not in (None, "") else "—"
            text = _clip(c, shown, col.width - GUTTER, font, self.BODY_SIZE)
            c.setFont(font, self.BODY_SIZE)
            c.drawString(x, self.y, text)
            x += col.width

        c.setStrokeColorRGB(*RULE)
        c.setLineWidth(0.25)
        c.line(self.PAGE_MARGIN, self.y - 7, self.width - self.PAGE_MARGIN, self.y - 7)
        self.y -= self.ROW_HEIGHT

    def total(self, count: int) -> None:
        self.y -= 6
        if self.y < self.FOOTER_HEIGHT + 26:
            self.new_page()
        self.c.setFillColorRGB(*INK)
        self.c.setFont("Helvetica-Bold", 9.5)
        self.c.drawString(self.PAGE_MARGIN, self.y, f"{count} payees in this report")


def build_pdf(rows: list[dict], *, scope: str, actor: str, channel_labels: dict) -> bytes:
    """Render the payee rows as a PDF and return its bytes.

    ``rows`` are the same records the Payees table shows. ``scope`` describes the filters
    that produced them, so the document says what it is a report *of* rather than leaving
    the reader to guess whether it is everything.

    ``actor`` is who asked for the export. It is not printed - a loopback address on a
    page names nobody - but it is still required, so that the caller cannot export without
    having identified the requester to the access log.
    """
    _ = actor
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
        "count": f"{len(rows)} payee{'' if len(rows) == 1 else 's'}",
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
