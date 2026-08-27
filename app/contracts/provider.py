"""ProviderDescriptor - what an image provider can do, and what it costs.

The rule that makes providers swappable: NO MODULE ANYWHERE MAY REFERENCE A
PROVIDER BY NAME.  The router selects on declared capability, cost and
measured quality priors.  Adding a provider is one adapter plus one
descriptor; nothing else changes.

Pricing lives here rather than in the ledger so that a price change is a
config edit, not a code change.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

DESCRIPTOR_VERSION = "1.0"


class Capability(str, Enum):
    TEXT_TO_IMAGE = "t2i"
    IMAGE_TO_IMAGE = "i2i"
    INPAINT = "inpaint"
    IDENTITY_REFERENCE = "identity_reference"
    POSE_CONTROL = "pose_control"
    UPSCALE = "upscale"


class Tier(str, Enum):
    """Which half of the two-stage design a provider serves.

    PREVIEW  cheap and fast; screened by free CPU checks only
    FINAL    quality; full gate, visual judge, repair loop
    """

    PREVIEW = "preview"
    FINAL = "final"


class PromptDialect(str, Enum):
    """How this provider likes to be spoken to.  The compiler renders the same
    AttributeIR differently per dialect - this is the only place the
    difference is expressed."""

    NATURAL_VERBOSE = "natural_verbose"  # flowing sentences
    TAG_WEIGHTED = "tag_weighted"  # comma-separated tags, weights
    INSTRUCTIONAL = "instructional"  # "change X to Y, keep everything else"


class ProviderDescriptor(BaseModel):
    version: str = DESCRIPTOR_VERSION
    id: str = Field(description="adapter id, e.g. 'mock.fast' - never used for routing logic")
    tiers: list[Tier] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)

    max_resolution: tuple[int, int] = (1024, 1024)
    cost_per_call_usd: float = Field(ge=0.0)
    p50_latency_s: float = Field(default=10.0, ge=0.0)
    prompt_dialect: PromptDialect = PromptDialect.NATURAL_VERBOSE

    #: Measured, not guessed.  Updated by the learning layer from real
    #: sessions.  Keys are check names from QAReport, values in [0, 1].
    quality_prior: dict[str, float] = Field(default_factory=dict)

    #: Bandit state: successes and attempts per look, filled at runtime.
    enabled: bool = True
    notes: str = ""

    def supports(self, *required: Capability) -> bool:
        owned = set(self.capabilities)
        return all(c in owned for c in required)

    def serves(self, tier: Tier) -> bool:
        return tier in self.tiers

    def prior(self, check_name: str, default: float = 0.5) -> float:
        return self.quality_prior.get(check_name, default)

    def estimate(self, calls: int) -> float:
        return self.cost_per_call_usd * calls


class GenerationRequest(BaseModel):
    """What an adapter is handed.  Provider-neutral by construction.

    ``prompt`` and ``negative_prompt`` are already rendered into this
    provider's dialect by the compiler; the adapter only maps fields onto an
    HTTP body.  An adapter that needs to make a creative decision is a sign
    the compiler is not doing its job.
    """

    prompt: str
    negative_prompt: str = ""
    source_image_path: str | None = None
    mask_path: str | None = None
    reference_image_paths: list[str] = Field(default_factory=list)

    width: int = 768
    height: int = 1024
    steps: int | None = None
    guidance: float | None = None
    strength: float | None = Field(default=None, ge=0.0, le=1.0)
    seed: int | None = None

    #: Free-form passthrough for provider-specific knobs the compiler learned
    #: are good for this look.  Opaque to everything except the adapter.
    extra: dict[str, object] = Field(default_factory=dict)


class GenerationResult(BaseModel):
    image_path: str
    provider_id: str
    cost_usd: float
    elapsed_s: float
    seed: int | None = None
    #: Everything needed to reproduce this image at full resolution later.
    #: Carried from preview to final so what she picked is what she receives.
    reproduction: dict[str, object] = Field(default_factory=dict)
