"""Credit balances, and warning her before they run out.

WHAT SHE ASKED FOR, in her own words:

    "Cuando los créditos de Anthropic o fal.ai estén llegando a un nivel bajo,
     quiero recibir una alerta indicando cuánto saldo queda y que es necesario
     recargar. También quiero que, cuando el saldo llegue a cero o no sea
     suficiente para generar una imagen, el bot se detenga y me avise
     inmediatamente, en lugar de continuar intentando generar imágenes.
     Y por favor, no actives ninguna recarga automática en mi tarjeta sin
     consultarme antes."

Three requirements, and the third is the one that decides the design:

  1. warn her while there is still credit, and say how much to add
  2. stop when it is gone - do not keep trying
  3. NEVER charge her card automatically

THE HONEST DIFFICULTY

Neither service publishes a "remaining credit" endpoint we can poll. Anthropic
and fal both tell you the balance is gone the same way: the next call fails.
So a number read from an API is not available, and inventing one would be
worse than none.

What IS available is what this system has spent, which it already records to
the last cent in the costs table - the same numbers her invoice is built
from. So she tells us what she topped up, and we count down from it. That is
honest arithmetic on a figure she supplied, not a guess dressed as a fact.

WHY NOTHING CAN CHARGE HER CARD

Worth stating plainly because she asked: this software has no payment
credentials of any kind. It holds two API keys, which can spend credit that
already exists and cannot buy more. Automatic top-up, if it exists at all,
is a setting inside her Anthropic and fal accounts - not something here can
switch on. The only way to add credit is for her to do it herself, on their
website.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Warn at a quarter left, and again at a tenth. Early enough to act on,
#: rarely enough not to become noise she stops reading.
WARN_FRACTION = 0.25
URGENT_FRACTION = 0.10

#: Below this there is not enough for a useful session, so treat it as empty
#: rather than letting her start something that will stop halfway.
#: One session is roughly 6 free previews plus 2-3 finals at $0.04.
MINIMUM_USABLE_USD = 0.20


@dataclass(frozen=True)
class Balance:
    """One service's credit, as far as we can honestly know it."""

    service: str
    topped_up_usd: float
    spent_usd: float

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.topped_up_usd - self.spent_usd)

    @property
    def fraction_left(self) -> float:
        if self.topped_up_usd <= 0:
            return 1.0  # nothing declared: nothing to warn about
        return self.remaining_usd / self.topped_up_usd

    @property
    def is_exhausted(self) -> bool:
        return self.topped_up_usd > 0 and self.remaining_usd < MINIMUM_USABLE_USD

    @property
    def level(self) -> str:
        if self.topped_up_usd <= 0:
            return "unknown"
        if self.is_exhausted:
            return "empty"
        if self.fraction_left <= URGENT_FRACTION:
            return "urgent"
        if self.fraction_left <= WARN_FRACTION:
            return "low"
        return "ok"

    def message_es(self) -> str | None:
        """What to tell her, in her language. None when there is nothing to say.

        Every message says the SAME three things, because a warning she has to
        interpret is a warning she will ignore: how much is left, what that
        means, and exactly what to do.
        """
        if self.level in {"ok", "unknown"}:
            return None

        # Round up to something she would actually type into a top-up box.
        suggested = max(5, int(self.topped_up_usd or 5))

        if self.level == "empty":
            return (
                f"Se ha acabado el saldo de {self.service}. He parado: no voy a "
                f"seguir intentando generar fotos. Para continuar, recarga en "
                f"{self.service} (con ${suggested} tienes de sobra). "
                "Nadie va a cobrar nada de tu tarjeta automaticamente."
            )
        if self.level == "urgent":
            return (
                f"Te queda muy poco saldo en {self.service}: "
                f"${self.remaining_usd:.2f}. Recarga pronto (${suggested} es "
                "suficiente) o dejare de poder generar fotos."
            )
        return (
            f"Te queda ${self.remaining_usd:.2f} de saldo en {self.service}. "
            f"Cuando puedas, recarga (${suggested} es suficiente)."
        )


class BalanceBook:
    """What she topped up, against what has been spent.

    The top-up figure is hers to state - we cannot read it from either
    service. The spend is ours, and it is the same figure her cost line and
    her invoice come from, so the two can never disagree.
    """

    #: Which recorded providers count against which service.
    SERVICES: dict[str, tuple[str, ...]] = {
        "fal.ai": ("fal.",),
        "Anthropic": ("claude", "judge", "analysis"),
    }

    def __init__(self, store) -> None:
        self._store = store

    def topped_up(self, service: str) -> float:
        try:
            return float(self._store.preference(f"topup:{service}", "0") or 0)
        except (TypeError, ValueError):
            return 0.0

    def set_topped_up(self, service: str, amount: float) -> None:
        """Record a top-up she has made. Additive, because that is what
        topping up is - the previous total does not vanish."""
        self._store.set_preference(
            f"topup:{service}", str(round(self.topped_up(service) + float(amount), 4))
        )
        # Spending since the last top-up is what we count against it.
        self._store.set_preference(f"topup_at:{service}", str(self._store_now()))

    def _store_now(self) -> float:
        import time

        return time.time()

    def spent_since_topup(self, service: str) -> float:
        try:
            since = float(self._store.preference(f"topup_at:{service}", "0") or 0)
        except (TypeError, ValueError):
            since = 0.0
        prefixes = self.SERVICES.get(service, ())
        return sum(
            float(row["usd"])
            for row in self._store.costs_since(since)
            if any(str(row["provider_id"]).startswith(p) or p in str(row["kind"])
                   for p in prefixes)
        )

    def balance(self, service: str) -> Balance:
        return Balance(
            service=service,
            topped_up_usd=self.topped_up(service),
            spent_usd=self.spent_since_topup(service),
        )

    def all(self) -> list[Balance]:
        return [self.balance(name) for name in self.SERVICES]

    def warnings_es(self) -> list[str]:
        return [m for m in (b.message_es() for b in self.all()) if m]

    def exhausted(self) -> list[str]:
        """Services with nothing usable left, so generation can stop rather
        than fail one call at a time."""
        return [b.service for b in self.all() if b.is_exhausted]
