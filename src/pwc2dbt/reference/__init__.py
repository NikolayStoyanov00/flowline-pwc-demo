"""A second implementation of the documented Informatica semantics.

Nothing in the converter imports this. It exists to disagree: the mapping is
executed row by row here, and :mod:`pwc2dbt.reference.compare` diffs the result
against what dbt built from the generated SQL. Two implementations that share a
parser and nothing else fail differently, which catches mistakes in either.

It shares the converter's reading of the Informatica documentation, so
agreement shows the two are consistent — not that either matches a real
PowerCenter run.
"""

from .compare import CompareError, Comparison, TableDiff, compare, compare_tables
from .evaluate import EvalError, coerce, evaluate
from .simulate import Result, SimulationError, VariableOrder, load_seeds, run_repository

__all__ = [
    "CompareError",
    "Comparison",
    "TableDiff",
    "compare",
    "compare_tables",
    "EvalError",
    "coerce",
    "evaluate",
    "Result",
    "SimulationError",
    "VariableOrder",
    "load_seeds",
    "run_repository",
]
