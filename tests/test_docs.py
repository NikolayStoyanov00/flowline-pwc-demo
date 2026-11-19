"""The documentation makes checkable claims; these check them.

`docs/REVIEW.md` walks a reviewer through the code by file, line and test
name. Those go stale silently as the code moves, and a reviewer following a
breakpoint into the wrong function has no way to tell it is the doc that is
wrong rather than their understanding.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REVIEW = PROJECT_ROOT / "docs" / "REVIEW.md"
README = PROJECT_ROOT / "README.md"
PACKAGE = PROJECT_ROOT / "src" / "pwc2dbt"
TESTS = PROJECT_ROOT / "tests"

#: `sql.py:750`, as written in a breakpoint table.
CITATION = re.compile(r"`(?P<module>[a-z_]+\.py):(?P<line>\d+)`")


def _collected_test_count() -> int:
    """How many tests pytest actually collects, parametrised cases included.

    A subprocess, because collecting from inside a running session would
    recurse. `--collect-only -q` prints one `path: count` line per file.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(TESTS)],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        timeout=300,
    )
    counts = re.findall(r"^\S+\.py: (\d+)$", result.stdout, re.MULTILINE)
    assert counts, f"could not read pytest's collection:\n{result.stdout[-600:]}"
    return sum(int(n) for n in counts)


def _definitions(module: Path) -> dict[int, str]:
    """Line number -> name, for every def and class in the module."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    return {
        node.lineno: node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _cited_lines() -> list[tuple[str, int, str]]:
    text = REVIEW.read_text(encoding="utf-8")
    cited = []
    for line in text.splitlines():
        for match in CITATION.finditer(line):
            cited.append((match["module"], int(match["line"]), line.strip()))
    return cited


class TestTheBreakpointsPointSomewhere:
    def test_the_review_cites_some_breakpoints(self) -> None:
        """Guard the guard: if the table is reworded away, this file should
        stop claiming to check it."""
        assert len(_cited_lines()) >= 4

    @pytest.mark.parametrize(
        ("module", "line", "context"),
        _cited_lines(),
        ids=lambda value: str(value) if not isinstance(value, str) else value[:20],
    )
    def test_each_cited_line_starts_a_definition(
        self, module: str, line: int, context: str
    ) -> None:
        path = PACKAGE / module
        assert path.is_file(), f"REVIEW.md cites {module}, which does not exist"

        definitions = _definitions(path)
        assert line in definitions, (
            f"REVIEW.md sends a reviewer to {module}:{line}, which is not the "
            f"first line of any function or class. Nearest above: "
            f"{max((n for n in definitions if n <= line), default='none')}. "
            f"Cited in: {context}"
        )

    def test_the_named_function_is_the_one_at_that_line(self) -> None:
        """The table reads ``sql.py:750`` ``_ModelBuilder.build`` — the name
        beside the citation has to be the definition it lands on."""
        for module, line, context in _cited_lines():
            name = _definitions(PACKAGE / module)[line]
            # The prose names it as `build` or `_ModelBuilder.build`.
            assert re.search(rf"\b{re.escape(name)}\b", context), (
                f"{module}:{line} is {name!r}, but the line describing it "
                f"names something else: {context}"
            )


class TestTheCitedTestsExist:
    def test_every_named_test_can_be_run(self) -> None:
        """REVIEW.md tells a reviewer which tests should fail when they break
        the join direction on purpose. Those names have to be real."""
        cited = set(re.findall(r"\btest_[a-z_]+", REVIEW.read_text(encoding="utf-8")))
        defined = set()
        for path in TESTS.glob("test_*.py"):
            defined.add(path.stem)
            defined |= {
                node.name
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.FunctionDef)
            }
        assert cited <= defined, f"REVIEW.md names tests that do not exist: {cited - defined}"


class TestTheTestCountIsCurrent:
    def test_the_readme_count_matches_the_suite(self) -> None:
        """A reviewer runs `pytest` and compares it to the README.

        Only the two places that describe the suite as it stands now. The
        write-up also recounts how many tests were green when a past bug got
        through, which is history and must not be updated.

        The real number comes from pytest's own collection, because counting
        `def test_` in the source misses every parametrised case and so lets
        a stale figure through.
        """
        text = README.read_text(encoding="utf-8")
        claimed = {
            int(n)
            for n in re.findall(r"`pytest` \| (\d+) tests", text)
            + re.findall(r"^tests/\s+(\d+) tests", text, re.MULTILINE)
        }
        assert claimed, "the README no longer states a test count"
        assert len(claimed) == 1, f"the README gives two different counts: {claimed}"

        collected = _collected_test_count()
        assert claimed == {collected}, (
            f"the README says {claimed.pop()} tests; pytest collects {collected}"
        )
