"""Credit balances - what the client asked for, in her own words.

    "Cuando los créditos de Anthropic o fal.ai estén llegando a un nivel bajo,
     quiero recibir una alerta indicando cuánto saldo queda... cuando el saldo
     llegue a cero... el bot se detenga y me avise inmediatamente, en lugar de
     continuar intentando generar imágenes. Y por favor, no actives ninguna
     recarga automática en mi tarjeta sin consultarme antes."

Three requirements. The third decides the design: this software holds two API
keys and no payment credentials, so it can spend credit that already exists
and cannot buy more. Nothing here can charge her card, and these tests are
partly a record of that.
"""

from __future__ import annotations

import pytest

from app.balance import MINIMUM_USABLE_USD, Balance, BalanceBook


def bal(topped: float, spent: float, service: str = "fal.ai") -> Balance:
    return Balance(service=service, topped_up_usd=topped, spent_usd=spent)


# ---------------------------------------------------------------------------
# 1. warn her while there is still time to act
# ---------------------------------------------------------------------------


def test_a_healthy_balance_says_nothing() -> None:
    """A warning that appears constantly is a warning she stops reading."""
    assert bal(10.0, 1.0).message_es() is None
    assert bal(10.0, 1.0).level == "ok"


def test_a_quarter_left_warns() -> None:
    warning = bal(10.0, 7.6).message_es()
    assert warning is not None
    assert "2.40" in warning
    assert "recarga" in warning.lower()


def test_a_tenth_left_is_urgent_and_says_so_differently() -> None:
    """The second warning must not read like the first, or she will assume she
    has already dealt with it."""
    low = bal(10.0, 7.6).message_es()
    urgent = bal(10.0, 9.2).message_es()
    assert low != urgent
    assert "poco" in urgent


def test_every_warning_says_how_much_and_what_to_do() -> None:
    """A warning she has to interpret is a warning she will ignore."""
    for spent in (7.6, 9.2, 9.95):
        message = bal(10.0, spent).message_es()
        assert message
        assert "recarga" in message.lower(), "must say what to do"
        assert "$" in message, "must say a figure"


def test_nothing_declared_means_nothing_to_warn_about() -> None:
    """She has not told us what she topped up. Inventing a number would be
    worse than saying nothing."""
    assert bal(0.0, 0.0).level == "unknown"
    assert bal(0.0, 5.0).message_es() is None


# ---------------------------------------------------------------------------
# 2. stop, rather than keep trying
# ---------------------------------------------------------------------------


def test_an_exhausted_balance_is_exhausted_before_it_hits_zero() -> None:
    """Stopping at exactly zero means she starts a session that dies halfway.
    Below one useful session's worth, treat it as empty."""
    assert bal(10.0, 10.0).is_exhausted
    assert bal(10.0, 10.0 - MINIMUM_USABLE_USD / 2).is_exhausted
    assert not bal(10.0, 5.0).is_exhausted


def test_the_empty_message_says_it_has_STOPPED() -> None:
    """She asked for the robot to stop and tell her, not to keep failing. The
    message has to say the stopping part, or she will not know it did."""
    message = bal(10.0, 10.0).message_es()
    assert "parado" in message
    assert "no voy a seguir" in message


def test_remaining_never_goes_negative() -> None:
    assert bal(10.0, 25.0).remaining_usd == 0.0


# ---------------------------------------------------------------------------
# 3. nothing may charge her card
# ---------------------------------------------------------------------------


def test_the_empty_message_reassures_her_about_the_card() -> None:
    """She asked explicitly. The moment she is most worried about money is the
    moment to say it."""
    assert "tarjeta" in bal(10.0, 10.0).message_es()


def test_there_is_no_way_to_add_credit_from_here() -> None:
    """A record of the design, not just a test.

    BalanceBook records what she topped up somewhere else. If a method ever
    appears here that talks to a payment API, this fails - and it should.
    """
    forbidden = ("charge", "pay", "purchase", "topup_card", "recharge", "billing")
    for name in dir(BalanceBook):
        if name.startswith("_"):
            continue
        assert not any(f in name.lower() for f in forbidden), (
            f"BalanceBook.{name} looks like it could spend money"
        )


# ---------------------------------------------------------------------------
# the book, against a real store
# ---------------------------------------------------------------------------


@pytest.fixture()
def book(tmp_path):
    from app.store import Store

    return BalanceBook(Store(tmp_path / "t.sqlite3"))


def test_a_top_up_adds_rather_than_replaces(book: BalanceBook) -> None:
    """Topping up twice means she has put in both amounts. Replacing would
    quietly lose the first one."""
    book.set_topped_up("fal.ai", 5.0)
    book.set_topped_up("fal.ai", 10.0)
    assert book.topped_up("fal.ai") == pytest.approx(15.0)


def test_spend_is_counted_per_service(book: BalanceBook) -> None:
    """fal spending must not eat her Anthropic credit."""
    import time

    now = time.time()
    book.set_topped_up("fal.ai", 10.0)
    book.set_topped_up("Anthropic", 5.0)
    store = book._store
    store.add_cost(session_id="s", kind="final", provider_id="fal.flux-kontext",
                   usd=2.0, detail="", at=now + 1)
    store.add_cost(session_id="s", kind="judge", provider_id="claude-haiku-4-5",
                   usd=0.5, detail="", at=now + 1)

    assert book.balance("fal.ai").spent_usd == pytest.approx(2.0)
    assert book.balance("Anthropic").spent_usd == pytest.approx(0.5)


def test_spending_before_a_top_up_does_not_count_against_it(book: BalanceBook) -> None:
    """Money spent last month is not spent out of today's top-up."""
    import time

    store = book._store
    store.add_cost(session_id="s", kind="final", provider_id="fal.flux-kontext",
                   usd=3.0, detail="", at=time.time() - 86_400)
    book.set_topped_up("fal.ai", 10.0)
    assert book.balance("fal.ai").spent_usd == pytest.approx(0.0)
    assert book.balance("fal.ai").remaining_usd == pytest.approx(10.0)


def test_exhausted_lists_only_what_is_actually_empty(book: BalanceBook) -> None:
    import time

    book.set_topped_up("fal.ai", 1.0)
    book.set_topped_up("Anthropic", 10.0)
    book._store.add_cost(session_id="s", kind="final",
                         provider_id="fal.flux-kontext", usd=1.0,
                         detail="", at=time.time() + 1)
    assert book.exhausted() == ["fal.ai"]


# ---------------------------------------------------------------------------
# Spending must reach the database, or three separate things undercount
# ---------------------------------------------------------------------------


def test_preview_spend_is_persisted_even_if_she_picks_nothing() -> None:
    """Costs were written only at the END of /finals.

    So a session where she looked at her options and picked nothing spent real
    money that never reached the costs table - and THREE things read that
    table: the spend figures she is shown, the daily cap after a restart, and
    the credit countdown she asked for. All three undercounted, and the error
    grew every time she changed her mind.
    """
    import inspect

    import app.main as main

    previews = inspect.getsource(main.start_previews)
    assert "_persist_costs" in previews, (
        "preview spending is not written to the database"
    )


def test_costs_are_not_written_twice() -> None:
    """previews persists, then finals persists again over the same session.
    Without deduplication her spend would double and the caps would fire at
    half the real limit."""
    import inspect

    import app.main as main

    source = inspect.getsource(main._persist_costs)
    assert "_PERSISTED_COSTS" in source
    assert "continue" in source, "no skip for an already-written entry"


def test_the_three_readers_share_one_table() -> None:
    """The spend she sees, the cap that stops her, and the balance countdown
    must be the same arithmetic - or they will disagree in front of her."""
    import inspect

    import app.main as main
    from app.balance import BalanceBook
    from app.store import Store

    assert "costs_since" in inspect.getsource(BalanceBook.spent_since_topup)
    assert "costs" in inspect.getsource(Store.spend_since)
    assert "costs_since" in inspect.getsource(main.Services.__init__)
