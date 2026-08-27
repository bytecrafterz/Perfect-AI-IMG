"""The router - which provider serves this request.

Selection is by DECLARED CAPABILITY, never by name.  Among the providers that
can do the job, choice is a cost-aware Thompson sampling bandit whose reward
is CHOSEN-PER-EURO rather than merely passing the gate.

That reward is the whole point.  A provider whose images sail through the
quality gate but which she never picks is not a good provider - it is an
expensive one.  Her taps are the only ground truth about taste, and they are
free, so the bandit learns from them.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable

from app.contracts.look_recipe import LookRecipe
from app.contracts.provider import Capability, Tier
from app.providers.base import ImageProvider, ProviderError, Registry


@dataclass
class Arm:
    """Beta-Bernoulli state for one (look, provider) pair.

    Priors of 1/1 are uniform: a new provider is neither trusted nor
    distrusted, and roughly a dozen sessions are enough to separate it from a
    bad one - which matches the "works from about the tenth session" claim in
    the spec rather than promising learning that needs thousands of samples.
    """

    successes: float = 1.0
    failures: float = 1.0

    def sample(self, rng: random.Random) -> float:
        return rng.betavariate(max(1e-6, self.successes), max(1e-6, self.failures))

    def update(self, *, shown: int, kept: int) -> None:
        if shown <= 0:
            return
        kept = max(0, min(kept, shown))
        self.successes += kept
        self.failures += shown - kept

    @property
    def observations(self) -> float:
        return self.successes + self.failures - 2.0

    @property
    def mean(self) -> float:
        return self.successes / (self.successes + self.failures)


@dataclass
class Bandit:
    """Arms keyed by (look_id, provider_id)."""

    arms: dict[tuple[str, str], Arm] = field(default_factory=dict)

    def arm(self, look_id: str, provider_id: str) -> Arm:
        return self.arms.setdefault((look_id, provider_id), Arm())

    def observe(self, *, look_id: str, provider_id: str, shown: int, kept: int) -> None:
        self.arm(look_id, provider_id).update(shown=shown, kept=kept)

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {
            f"{look_id}|{provider_id}": {
                "successes": arm.successes,
                "failures": arm.failures,
                "mean": arm.mean,
                "observations": arm.observations,
            }
            for (look_id, provider_id), arm in self.arms.items()
        }

    def load(self, snapshot: dict[str, dict[str, float]]) -> None:
        for key, values in snapshot.items():
            look_id, _, provider_id = key.partition("|")
            self.arms[(look_id, provider_id)] = Arm(
                successes=float(values.get("successes", 1.0)),
                failures=float(values.get("failures", 1.0)),
            )


@dataclass
class Choice:
    provider: ImageProvider
    reason: str
    expected_keep_rate: float
    unit_cost_usd: float


class Router:
    def __init__(
        self,
        registry: Registry,
        *,
        bandit: Bandit | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._registry = registry
        self.bandit = bandit or Bandit()
        self._rng = rng or random.Random()

    def required_capabilities(
        self, look: LookRecipe | None, *, has_source: bool
    ) -> tuple[Capability, ...]:
        """What this job actually needs, derived from the route hint.

        The mapping the spec describes:
          garment / background / lighting change  -> edit the real photo
          new pose or full-body scene             -> reference or LoRA
          background plates with no subject       -> plain generation
        """
        hint = (look.route_hint if look else "in_place_edit").lower()
        if hint == "in_place_edit" and has_source:
            return (Capability.IMAGE_TO_IMAGE,)
        if hint == "identity_reference":
            return (Capability.IDENTITY_REFERENCE,)
        if hint == "scene_only":
            return (Capability.TEXT_TO_IMAGE,)
        return (Capability.IMAGE_TO_IMAGE,) if has_source else (Capability.TEXT_TO_IMAGE,)

    def select(
        self,
        *,
        tier: Tier,
        look: LookRecipe | None = None,
        has_source: bool = True,
        required: Iterable[Capability] | None = None,
        exclude: Iterable[str] = (),
    ) -> Choice:
        """Pick a provider, or explain precisely why none fits."""
        needed = tuple(required) if required is not None else self.required_capabilities(
            look, has_source=has_source
        )
        excluded = set(exclude)

        candidates = [
            p
            for p in self._registry.candidates(tier=tier, required=needed)
            if p.descriptor.id not in excluded
        ]
        if not candidates:
            raise ProviderError(
                "router",
                f"no provider serves tier={tier.value} with "
                f"capabilities={[c.value for c in needed]}",
                retryable=False,
            )

        look_id = look.id if look else "_global"
        best: Choice | None = None
        for provider in candidates:
            descriptor = provider.descriptor
            arm = self.bandit.arm(look_id, descriptor.id)
            sampled = arm.sample(self._rng)

            # Cost-aware: value per euro, not value alone.  A free provider is
            # not automatically best - a slightly dearer one she keeps twice as
            # often is the better buy, and this expresses that.
            unit = descriptor.cost_per_call_usd
            value = sampled / unit if unit > 0 else sampled * 1_000.0

            if best is None or value > best.expected_keep_rate:
                best = Choice(
                    provider=provider,
                    reason=(
                        f"tier={tier.value} caps={[c.value for c in needed]} "
                        f"keep~{arm.mean:.2f} n={int(arm.observations)} "
                        f"${unit:.4f}/call"
                    ),
                    expected_keep_rate=value,
                    unit_cost_usd=unit,
                )
        assert best is not None
        return best

    def provider_for_reproduction(self, provider_id: str) -> ImageProvider:
        """A chosen preview's final MUST come from the provider that made it.

        Routing afresh here would hand her a different image from the one she
        picked, which breaks the only promise the two-stage design makes.
        """
        return self._registry.get(provider_id)
