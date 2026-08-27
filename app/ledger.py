"""Cost accounting and spending caps.

Two jobs:

  1. Every call is priced from its ProviderDescriptor, so a price change is a
     config edit rather than a code change.
  2. The caps REFUSE rather than overspend.  Autonomous batches spend money
     while she is not watching, which is exactly when a runaway loop is most
     expensive, so the check happens before the spend and not after.

The ledger is the source of the cost line on every delivery.  She always
knows what a session cost, because the number comes from the same place the
money did.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class CapKind(str, Enum):
    SESSION = "session"
    DAILY = "daily"
    BALANCE = "balance"


class BudgetExceeded(Exception):
    """Raised before spending, never after.

    Carries which cap was hit so the message to her can be specific rather
    than a generic failure.
    """

    def __init__(self, kind: CapKind, limit: float, would_be: float) -> None:
        self.kind = kind
        self.limit = limit
        self.would_be = would_be
        super().__init__(
            f"{kind.value} cap {limit:.2f} would be exceeded ({would_be:.2f})"
        )

    def message_es(self) -> str:
        if self.kind is CapKind.SESSION:
            return (
                f"Esta sesion costaria unos ${self.would_be:.2f} y tu limite por "
                f"sesion es ${self.limit:.2f}. Prueba con menos opciones."
            )
        if self.kind is CapKind.DAILY:
            return (
                f"Hoy ya has usado casi todo tu limite diario de "
                f"${self.limit:.2f}. Manana se reinicia."
            )
        return (
            f"Te queda ${self.limit:.2f} de saldo y esta sesion costaria unos "
            f"${self.would_be:.2f}."
        )


@dataclass(frozen=True)
class Entry:
    """One priced call."""

    at: float
    session_id: str
    kind: str  # 'analyse' | 'preview' | 'final' | 'judge' | 'repair'
    provider_id: str
    usd: float
    detail: str = ""


@dataclass
class Ledger:
    """In-memory accumulator for one process.

    Persistence is the caller's job (see app/models.py); keeping this pure
    makes the arithmetic and the cap logic testable without a database.
    """

    per_session_usd: float
    per_day_usd: float
    balance_floor_usd: float = 0.0
    balance_usd: float | None = None  # None = unmetered (prepaid not in use)
    entries: list[Entry] = field(default_factory=list)
    _now: object = time.time  # injectable for tests

    # -- recording ---------------------------------------------------------

    def record(
        self,
        *,
        session_id: str,
        kind: str,
        provider_id: str,
        usd: float,
        detail: str = "",
    ) -> Entry:
        if usd < 0:
            raise ValueError("a cost cannot be negative")
        entry = Entry(
            at=float(self._now()),  # type: ignore[operator]
            session_id=session_id,
            kind=kind,
            provider_id=provider_id,
            usd=usd,
            detail=detail,
        )
        self.entries.append(entry)
        if self.balance_usd is not None:
            self.balance_usd -= usd
        return entry

    # -- views -------------------------------------------------------------

    def session_total(self, session_id: str) -> float:
        return sum(e.usd for e in self.entries if e.session_id == session_id)

    def day_total(self, *, window_s: float = 86_400.0) -> float:
        cutoff = float(self._now()) - window_s  # type: ignore[operator]
        return sum(e.usd for e in self.entries if e.at >= cutoff)

    def total(self) -> float:
        return sum(e.usd for e in self.entries)

    def by_kind(self, session_id: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.entries:
            if e.session_id == session_id:
                out[e.kind] = out.get(e.kind, 0.0) + e.usd
        return out

    def cost_per_delivered(self, session_id: str, delivered: int) -> float | None:
        """The number that actually matters.

        Not cost per generation - cost per photo she keeps, which includes
        every preview she did not choose.
        """
        if delivered <= 0:
            return None
        return self.session_total(session_id) / delivered

    # -- the gate on spending ---------------------------------------------

    def check(self, *, session_id: str, additional_usd: float) -> None:
        """Raise if this spend would breach any cap.

        Called BEFORE the work, with the estimate.  A batch that cannot afford
        to finish should never start, because a half-finished batch has spent
        real money and delivered nothing.
        """
        if additional_usd <= 0:
            return

        would_be_session = self.session_total(session_id) + additional_usd
        if would_be_session > self.per_session_usd:
            raise BudgetExceeded(
                CapKind.SESSION, self.per_session_usd, would_be_session
            )

        would_be_day = self.day_total() + additional_usd
        if would_be_day > self.per_day_usd:
            raise BudgetExceeded(CapKind.DAILY, self.per_day_usd, would_be_day)

        if self.balance_usd is not None:
            remaining = self.balance_usd - self.balance_floor_usd
            if additional_usd > remaining:
                raise BudgetExceeded(CapKind.BALANCE, max(0.0, remaining), additional_usd)

    def affordable(self, *, session_id: str, unit_usd: float, wanted: int) -> int:
        """How many of `wanted` units fit inside every cap.

        Used to trim a refill round rather than abandon it: delivering four
        good photos beats failing at six.
        """
        if unit_usd <= 0:
            return wanted
        headroom = min(
            self.per_session_usd - self.session_total(session_id),
            self.per_day_usd - self.day_total(),
        )
        if self.balance_usd is not None:
            headroom = min(headroom, self.balance_usd - self.balance_floor_usd)
        if headroom <= 0:
            return 0
        return max(0, min(wanted, int(headroom // unit_usd)))


@dataclass(frozen=True)
class SessionEstimate:
    """What she is shown before a batch starts.

    Deliberately an estimate with its parts visible, because the actual is
    shown on delivery and the two should be recognisably related.
    """

    previews: int
    preview_unit_usd: float
    analysis_usd: float
    expected_finals: int
    final_unit_usd: float
    judge_unit_usd: float

    @property
    def preview_stage_usd(self) -> float:
        return self.analysis_usd + self.previews * self.preview_unit_usd

    @property
    def final_stage_usd(self) -> float:
        return self.expected_finals * (self.final_unit_usd + self.judge_unit_usd)

    @property
    def total_usd(self) -> float:
        return self.preview_stage_usd + self.final_stage_usd

    def message_es(self) -> str:
        return f"Estimado: unos ${self.preview_stage_usd:.2f} para ver las opciones"
