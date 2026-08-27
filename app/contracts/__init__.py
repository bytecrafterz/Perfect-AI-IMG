"""The four contracts.

Everything crosses these.  No module outside this package may define an
equivalent shape, and no module anywhere may reference an image provider by
name - the router selects on declared capability.

    LookRecipe          a catalog entry: recipe, applies_to, axes, locks, chips
    AttributeIR         subject and scene; what is locked, what may move
    QAReport            verdict, per-check measurements, defects with boxes
    ProviderDescriptor  capabilities, resolution, cost, latency, quality priors

Plus Selections, which is what she marked on the style screen.
"""

from app.contracts.attribute_ir import (
    IR_VERSION,
    AttributeIR,
    BodyProportions,
    CaptureIR,
    FaceDescriptor,
    HairDescriptor,
    SceneIR,
    SkinTone,
    SubjectIR,
)
from app.contracts.common import (
    ALWAYS_LOCKED,
    SELECTABLE,
    Attribute,
    BBox,
    Framing,
    Money,
)
from app.contracts.look_recipe import (
    RECIPE_VERSION,
    AppliesTo,
    CameraSpec,
    ChipStat,
    GarmentSpec,
    LightingSpec,
    LookRecipe,
    LookStats,
    Recipe,
    SceneSpec,
)
from app.contracts.provider import (
    DESCRIPTOR_VERSION,
    Capability,
    GenerationRequest,
    GenerationResult,
    PromptDialect,
    ProviderDescriptor,
    Tier,
)
from app.contracts.qa_report import (
    REPORT_VERSION,
    Check,
    CheckOutcome,
    Defect,
    DefectKind,
    QAReport,
    Verdict,
)
from app.contracts.selections import RowState, Selections

__all__ = [
    # common
    "ALWAYS_LOCKED",
    "SELECTABLE",
    "Attribute",
    "BBox",
    "Framing",
    "Money",
    # attribute ir
    "IR_VERSION",
    "AttributeIR",
    "BodyProportions",
    "CaptureIR",
    "FaceDescriptor",
    "HairDescriptor",
    "SceneIR",
    "SkinTone",
    "SubjectIR",
    # look recipe
    "RECIPE_VERSION",
    "AppliesTo",
    "CameraSpec",
    "ChipStat",
    "GarmentSpec",
    "LightingSpec",
    "LookRecipe",
    "LookStats",
    "Recipe",
    "SceneSpec",
    # qa report
    "REPORT_VERSION",
    "Check",
    "CheckOutcome",
    "Defect",
    "DefectKind",
    "QAReport",
    "Verdict",
    # provider
    "DESCRIPTOR_VERSION",
    "Capability",
    "GenerationRequest",
    "GenerationResult",
    "PromptDialect",
    "ProviderDescriptor",
    "Tier",
    # selections
    "RowState",
    "Selections",
]
