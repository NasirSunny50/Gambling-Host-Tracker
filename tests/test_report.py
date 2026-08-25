"""The payee report as a PDF.

A CSV is read by a system; this is read by a person and may end up printed, photocopied or
attached to a case file. So the parts that matter here are the ones a loose page needs to
still mean something: what it is a report of, when it was taken, and which page it is.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("reportlab")
pypdf = pytest.importorskip("pypdf")

from ght.export.report import _latin_safe, build_pdf

NOW = datetime(2026, 8, 21, 18, 26, tzinfo=UTC)
LABELS = {"bkash": "bKash", "nagad": "Nagad"}


def row(**kw):
    fields = {"channel": "bkash", "number": "+8801700000000", "name": "A Holder",
              "bank": None, "site": "1xbet-bd", "times": 3, "last_seen": NOW}
    return {**fields, **kw}


def read(pdf: bytes):
    from io import BytesIO

    reader = pypdf.PdfReader(BytesIO(pdf))
    return reader, "\n".join(page.extract_text() for page in reader.pages)


def test_every_page_says_what_it_is_and_which_page_it_is():
    """Pages get separated from their cover sheet. Each one has to stand alone."""
    pdf = build_pdf([row() for _ in range(60)], scope="All payees, all sites",
                    actor="127.0.0.1", channel_labels=LABELS)
    reader, _ = read(pdf)
    assert len(reader.pages) > 1

    for number, page in enumerate(reader.pages, start=1):
        text = page.extract_text()
        assert "Payee report" in text
        assert "All payees, all sites" in text
        assert f"Page {number}" in text


def test_the_report_names_the_filters_behind_it():
    """A filtered report that does not say so reads as the whole picture."""
    pdf = build_pdf([row()], scope="Filtered to run #48, Nagad", actor="127.0.0.1",
                    channel_labels=LABELS)
    _, text = read(pdf)
    assert "Filtered to run #48, Nagad" in text


def test_it_states_how_many_rows_but_not_the_address_that_asked():
    """The count is the reader's check that they have the whole set. The requester's IP is
    not printed: a loopback address names nobody, and the access log is where who-exported-
    what is actually answerable."""
    pdf = build_pdf([row(), row()], scope="All payees, all sites", actor="10.0.0.9",
                    channel_labels=LABELS)
    _, text = read(pdf)
    assert "2 payees in this report" in text
    assert "10.0.0.9" not in text


def test_a_name_only_payee_appears_with_no_number():
    pdf = build_pdf([row(number=None, name="ALADDIN EXPRESS", channel="nagad")],
                    scope="All payees, all sites", actor="127.0.0.1", channel_labels=LABELS)
    _, text = read(pdf)
    assert "ALADDIN EXPRESS" in text
    assert "Nagad" in text


def test_an_empty_report_is_still_a_valid_document():
    """Someone filters to nothing and prints it: the page must still say what it looked for."""
    pdf = build_pdf([], scope='Filtered to matching "nothing"', actor="127.0.0.1",
                    channel_labels=LABELS)
    reader, text = read(pdf)
    assert len(reader.pages) == 1
    assert "0 payees in this report" in text


def test_a_name_the_font_cannot_draw_is_not_faked():
    """Helvetica maps unknown code points onto whatever glyph sits at that byte, so a
    Bengali name comes out as confident nonsense. Question marks are honest."""
    assert _latin_safe("মেসার্স", "Helvetica") == "???????"
    # With a Unicode face registered the name goes through untouched.
    assert _latin_safe("মেসার্স", "Nirmala") == "মেসার্স"
    assert _latin_safe("Plain Name", "Helvetica") == "Plain Name"
