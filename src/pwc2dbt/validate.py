"""Checks that an input file really is a PowerCenter export before parsing it.

Keeping these out of ``parse.py`` leaves the parsing functions as a flat
sequence of "read this element into that dataclass", with no error handling
interleaved.
"""

from __future__ import annotations

from xml.etree import ElementTree

__all__ = ["ParseError", "validate_envelope"]

ROOT_TAG = "POWERMART"
REPOSITORY_TAG = "REPOSITORY"
FOLDER_TAG = "FOLDER"


class ParseError(Exception):
    """The input is not a PowerCenter export we can read."""


def validate_envelope(
    root: ElementTree.Element,
) -> tuple[ElementTree.Element, ElementTree.Element]:
    """Check the ``POWERMART > REPOSITORY > FOLDER`` envelope.

    Returns the ``REPOSITORY`` and ``FOLDER`` elements so the caller does not
    have to look them up again.
    """
    if root.tag != ROOT_TAG:
        raise ParseError(
            f"expected a <{ROOT_TAG}> root element, found <{root.tag}>; "
            "this does not look like a PowerCenter repository export"
        )

    repository = root.find(REPOSITORY_TAG)
    if repository is None:
        raise ParseError(f"no <{REPOSITORY_TAG}> element found")

    folder = repository.find(FOLDER_TAG)
    if folder is None:
        raise ParseError(f"no <{FOLDER_TAG}> element found")

    return repository, folder
