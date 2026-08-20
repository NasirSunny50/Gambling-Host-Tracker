"""Paging the payee lists.

The arithmetic is small but it is what the reader trusts: "showing 51-100 of 214" is a
claim about the filtered set, and an off-by-one there is a claim that the data is different
from what it is.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ght.api.routes import Page, _paginate
from ght.models import Account, Base


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _add_accounts(session, count, channel="bkash"):
    for i in range(count):
        session.add(
            Account(
                channel=channel,
                account_number=f"+88017000000{i:02d}",
                bank_key="",
                confidence=0.9,
                is_active=True,
            )
        )
    session.flush()


# ----------------------------------------------------------------------- arithmetic


def test_page_describes_its_slice():
    page = Page(number=3, size=50, total=214)
    assert page.pages == 5
    assert page.offset == 100
    assert page.first_row == 101
    assert page.last_row == 150
    assert page.has_prev is True
    assert page.has_next is True


def test_last_page_stops_at_the_total():
    page = Page(number=5, size=50, total=214)
    assert page.first_row == 201
    assert page.last_row == 214  # not 250
    assert page.has_next is False


def test_a_single_short_page():
    page = Page(number=1, size=50, total=7)
    assert page.pages == 1
    assert (page.first_row, page.last_row) == (1, 7)
    assert page.has_prev is False and page.has_next is False


def test_an_empty_result_still_describes_itself():
    page = Page(number=1, size=50, total=0)
    assert page.pages == 1
    assert page.first_row == 0
    assert page.last_row == 0


def test_an_exact_multiple_does_not_add_a_trailing_page():
    assert Page(number=1, size=50, total=100).pages == 2


# ------------------------------------------------------------------------ slicing


def test_paginate_returns_the_right_slice(session, monkeypatch):
    monkeypatch.setattr("ght.api.routes.PAGE_SIZE", 10)
    _add_accounts(session, 25)

    stmt = select(Account).order_by(Account.account_number)
    rows, page = _paginate(session, stmt, 2)

    assert page.total == 25
    assert len(rows) == 10
    assert rows[0].account_number.endswith("10")


def test_the_count_reflects_the_filter_not_the_table(session, monkeypatch):
    """"of 214" has to mean the filtered set, or the number is a lie."""
    monkeypatch.setattr("ght.api.routes.PAGE_SIZE", 10)
    _add_accounts(session, 20, channel="bkash")
    _add_accounts(session, 5, channel="nagad")

    stmt = select(Account).where(Account.channel == "nagad")
    rows, page = _paginate(session, stmt, 1)

    assert page.total == 5
    assert len(rows) == 5


def test_a_page_past_the_end_clamps_to_the_last_one(session, monkeypatch):
    """A bookmarked page, or a set that shrank, should not look like "nothing matches"."""
    monkeypatch.setattr("ght.api.routes.PAGE_SIZE", 10)
    _add_accounts(session, 12)

    rows, page = _paginate(session, select(Account), 99)

    assert page.number == 2
    assert len(rows) == 2


def test_an_empty_set_gives_page_one_and_no_rows(session, monkeypatch):
    monkeypatch.setattr("ght.api.routes.PAGE_SIZE", 10)
    rows, page = _paginate(session, select(Account), 1)
    assert rows == []
    assert page.number == 1 and page.total == 0
