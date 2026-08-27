"""Provider adapters and the registry.

THE RULE THIS MODULE EXISTS TO ENFORCE: no code outside app/providers/ may
name a provider.  Everything upstream asks for a capability and a tier and
gets back whatever is registered and best.  Adding a provider is one adapter
plus one descriptor; swapping one is a config edit.

An adapter maps a GenerationRequest onto an HTTP body and back.  If an adapter
finds itself making a creative decision - choosing words, picking a style -
that belongs in the compiler instead.
"""

from __future__ import annotations

import abc
from typing import Iterable

from app.contracts.provider import (
    Capability,
    GenerationRequest,
    GenerationResult,
    ProviderDescriptor,
    Tier,
)


class ProviderError(RuntimeError):
    """A provider failed in a way the orchestrator may retry around."""

    def __init__(self, provider_id: str, detail: str, *, retryable: bool = True) -> None:
        self.provider_id = provider_id
        self.retryable = retryable
        super().__init__(f"{provider_id}: {detail}")


class ImageProvider(abc.ABC):
    """One image generation backend."""

    @property
    @abc.abstractmethod
    def descriptor(self) -> ProviderDescriptor:
        """Capabilities, cost and measured priors.  Read by the router; never
        interpreted by anything else."""

    @abc.abstractmethod
    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Produce one image.

        Must populate ``reproduction`` with everything needed to recreate this
        image at full resolution later.  That dictionary is what carries a
        chosen preview into its final, and it is the reason what she picked is
        what she receives.
        """

    async def inpaint(self, request: GenerationRequest) -> GenerationResult:
        """Repaint the masked region only.

        Default implementation refuses rather than silently regenerating the
        whole frame, which would destroy the identity the gate has already
        validated on the rest of the image.
        """
        raise ProviderError(
            self.descriptor.id, "does not support inpainting", retryable=False
        )

    async def close(self) -> None:  # pragma: no cover - trivial
        return None


class Registry:
    """Everything available, queried by capability rather than by name."""

    def __init__(self) -> None:
        self._providers: dict[str, ImageProvider] = {}

    def register(self, provider: ImageProvider) -> None:
        self._providers[provider.descriptor.id] = provider

    def all(self) -> list[ImageProvider]:
        return [p for p in self._providers.values() if p.descriptor.enabled]

    def get(self, provider_id: str) -> ImageProvider:
        """By id - only for reproducing a specific earlier result.

        A preview's final must come from the same provider that made the
        preview, or fidelity is lost. That is the sole legitimate use.
        """
        try:
            return self._providers[provider_id]
        except KeyError:
            raise ProviderError(
                provider_id, "provider is not registered", retryable=False
            ) from None

    def candidates(
        self, *, tier: Tier, required: Iterable[Capability] = ()
    ) -> list[ImageProvider]:
        required = tuple(required)
        return [
            p
            for p in self.all()
            if p.descriptor.serves(tier) and p.descriptor.supports(*required)
        ]

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()


registry = Registry()
