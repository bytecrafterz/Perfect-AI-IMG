"""Build the provider registry from providers.json.

The whole point of this module is that it is the ONLY place a provider name
appears outside its own adapter. Everything downstream asks the router for a
capability and a tier.

Behaviour that matters operationally:

  * an entry whose API key is not set is SKIPPED, not failed. The file can
    list everything and the machine runs whatever it has keys for.
  * if nothing is configured, the local mock is registered so the app still
    works end to end - and says loudly that it is not producing photographs.
  * a malformed entry is skipped with a message rather than taking down the
    whole registry. One stray comma should not cost her every provider.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.contracts.provider import Capability, PromptDialect, Tier
from app.providers.base import ImageProvider, Registry
from app.providers.mock import build_mock_providers


class LoadReport:
    """What was loaded, what was skipped, and why - surfaced on /health and
    the settings screen rather than buried in a log."""

    def __init__(self) -> None:
        self.loaded: list[str] = []
        self.skipped: list[tuple[str, str]] = []
        self.using_mock: bool = False

    def messages_es(self) -> list[str]:
        out: list[str] = []
        if self.using_mock:
            out.append(
                "Sin proveedor de imagen configurado: se usa el generador de "
                "prueba local, que no produce fotografias reales"
            )
        for provider_id, reason in self.skipped:
            if "falta" in reason:
                continue  # an unset key is expected, not a problem to report
            out.append(f"Proveedor {provider_id} no cargado: {reason}")
        return out


def _tier(value: str) -> Tier:
    return Tier(value)


def _capability(value: str) -> Capability:
    return Capability(value)


def build_registry(
    *,
    config_path: Path,
    output_dir: Path,
    env: dict[str, str] | None = None,
) -> tuple[Registry, LoadReport]:
    env = env if env is not None else dict(os.environ)
    registry = Registry()
    report = LoadReport()

    entries = _read_entries(config_path, report)

    for entry in entries:
        try:
            provider = _build_one(entry, output_dir=output_dir, env=env, report=report)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not lose the rest
            report.skipped.append((str(entry.get("id", "?")), f"error: {exc}"))
            continue
        if provider is not None:
            registry.register(provider)
            report.loaded.append(provider.descriptor.id)

    if not report.loaded:
        for provider in build_mock_providers(output_dir):
            registry.register(provider)
            report.loaded.append(provider.descriptor.id)
        report.using_mock = True

    return registry, report


def _read_entries(config_path: Path, report: LoadReport) -> list[dict]:
    if not config_path.exists():
        report.skipped.append((str(config_path), "falta el fichero de proveedores"))
        return []
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.skipped.append((str(config_path), f"JSON invalido: {exc}"))
        return []
    entries = payload.get("providers")
    return entries if isinstance(entries, list) else []


def _build_one(
    entry: dict, *, output_dir: Path, env: dict[str, str], report: LoadReport
) -> ImageProvider | None:
    provider_id = str(entry["id"])

    if not entry.get("enabled", True):
        report.skipped.append((provider_id, "desactivado en providers.json"))
        return None

    key_name = str(entry.get("api_key_env", ""))
    api_key = env.get(key_name, "").strip()
    if not api_key:
        report.skipped.append((provider_id, f"falta {key_name}"))
        return None

    common = dict(
        provider_id=provider_id,
        model=str(entry["model"]),
        tiers=[_tier(t) for t in entry.get("tiers", [])],
        capabilities=[_capability(c) for c in entry.get("capabilities", [])],
        cost_per_call_usd=float(entry["cost_per_call_usd"]),
        output_dir=output_dir,
        p50_latency_s=float(entry.get("p50_latency_s", 8.0)),
        max_resolution=tuple(entry.get("max_resolution", (1024, 1024))),  # type: ignore[arg-type]
        prompt_dialect=PromptDialect(entry.get("prompt_dialect", "natural_verbose")),
        quality_prior=entry.get("quality_prior") or {},
        extra_params=entry.get("params") or {},
    )

    adapter = str(entry.get("adapter", "")).lower()

    if adapter == "fal":
        from app.providers.fal import FalProvider

        return FalProvider(
            api_key=api_key,
            inpaint_model=entry.get("inpaint_model"),
            i2i_model=entry.get("i2i_model"),
            **common,
        )

    if adapter == "replicate":
        from app.providers.replicate import ReplicateProvider

        return ReplicateProvider(
            api_token=api_key,
            version=entry.get("version"),
            **common,
        )

    report.skipped.append((provider_id, f"adaptador desconocido '{adapter}'"))
    return None
