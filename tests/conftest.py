"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from pwc2dbt.model import Repository
from pwc2dbt.parse import parse_file

#: Repository root, i.e. the directory holding the PowerCenter export.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: The sample export this exercise is built against.
DEMO_XML = PROJECT_ROOT / "FLOWLINE_DEMO_JAFFLESHOP.xml"


@pytest.fixture(scope="session")
def demo_xml_path() -> Path:
    if not DEMO_XML.is_file():
        pytest.skip(f"sample export not found at {DEMO_XML}")
    return DEMO_XML


@pytest.fixture(scope="session")
def repo(demo_xml_path: Path) -> Repository:
    """The parsed sample export.

    Session-scoped: parsing 4,700 lines takes a moment and the result is
    immutable, so every test can share one instance.
    """
    return parse_file(demo_xml_path)
