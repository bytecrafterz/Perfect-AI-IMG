"""Test isolation.

Runs BEFORE any app import, because app.config reads the environment once at
import time and pytest loads conftest first.

This exists because of a real surprise: running scripts/build_profile.py wrote
a profile into data/, and the web tests - which construct the app's real
Services - silently started measuring generated images against a skin
reference built from unrelated photos. Everything still "worked", finals were
just quietly discarded.

Tests must not read or write whatever happens to be in the working data
directory, and the failure mode when they do is a passing suite that stops
matching reality.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DATA = Path(tempfile.mkdtemp(prefix="estudio-tests-"))

# Set before app.config is imported anywhere.
os.environ["DATA_DIR"] = str(_TEST_DATA)
os.environ.setdefault("OWNER_NAME", "Nayane")
# Left unset deliberately: the tests assert that a half-configured system
# announces itself, and that photographs are never public.
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("FAL_API_KEY", None)
os.environ.pop("REPLICATE_API_TOKEN", None)
os.environ.pop("ACCESS_TOKEN", None)


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    import shutil

    shutil.rmtree(_TEST_DATA, ignore_errors=True)
