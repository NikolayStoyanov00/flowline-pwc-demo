"""A reference implementation of the documented Informatica semantics.

Executes a PowerCenter mapping row by row, the way the Integration Service
does, and writes the same target tables the converted dbt models produce. The
two can then be compared.

**What this does and does not prove.** It was written from the same reading of
Informatica's documentation as the converter, so agreement between them shows
the two implementations are consistent — it does not show that either matches
a real PowerCenter run. Nothing here has been tested against PowerCenter,
because PowerCenter is licensed software that cannot be installed to check.

It is still worth having: it shares no code with the converter and fails
differently, the way an interpreter and a compiler do, so it catches
implementation mistakes in either.

Known places where a real run could differ are listed in the README.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping as MappingABC, Sequence

from .evaluate import AGGREGATES, EvalError, aggregate, coerce, evaluate
from .expressions import (
    Call,
    Node,
    parse_expression,
    stateful_variable_ports,
)
from .graph import build_graph
from .naming import is_numeric, is_string
from .model import Connector, Instance, Kind, Mapping, Port, Repository, Transformation

__all__ = ["SimulationError", "Result", "run_repository", "load_seeds", "VariableOrder"]


class SimulationError(Exception):
    """The mapping cannot be executed."""


class VariableOrder:
    """How to order the evaluation of local variable ports.

    PowerCenter evaluates them top to bottom in Ports-tab order, and the XML
    appears to preserve that order. Whether it really does is the one semantic
    question in this export that cannot be settled without PowerCenter, and it
    decides whether a running counter ever resets, so both readings are
    available here.
    """

    #: As the XML declares them. The literal reading: a variable assigned a
    #: bare column is updated before anything reads it, so it holds the
    #: *current* row.
    DOCUMENT = "document"

    #: Variables that are a bare column reference are updated last, so when
    #: another variable reads one it still holds the *previous* row's value.
    #: This is what the "remember the previous row" idiom needs in order to
    #: work, and what the port names imply was intended.
    DEFERRED = "deferred"


@dataclass
class Result:
    """What a simulated run produced."""

    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Source data
# ---------------------------------------------------------------------------


def load_seeds(repo: Repository, seeds_dir: Path | str) -> dict[str, list[dict]]:
    """Read the seed CSVs as the mapping's source tables.

    Values are coerced using each SOURCE definition's datatypes, because a CSV
    is all strings and the expressions expect numbers and timestamps.
    """
    seeds_dir = Path(seeds_dir)
    loaded: dict[str, list[dict]] = {}

    for table in repo.sources.values():
        path = seeds_dir / f"{table.name.lower()}.csv"
        if not path.is_file():
            continue
        types = {column.name: column.datatype for column in table.columns}
        rows: list[dict[str, Any]] = []
        with path.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                rows.append(
                    {
                        name.upper(): coerce(value, types.get(name.upper(), "string"))
                        for name, value in raw.items()
                    }
                )
        loaded[table.name] = rows

    return loaded


# ---------------------------------------------------------------------------
# One mapping
# ---------------------------------------------------------------------------

#: Rows of one instance, per output group. Non-router steps use "".
Grouped = dict[str, list[dict[str, Any]]]


class _MappingRun:
    def __init__(
        self,
        repo: Repository,
        mapping: Mapping,
        sources: MappingABC[str, list[dict]],
        parameters: MappingABC[str, Any],
        variable_order: str,
    ) -> None:
        self.repo = repo
        self.mapping = mapping
        self.graph = build_graph(mapping)
        self.sources = sources
        self.parameters = parameters
        self.variable_order = variable_order
        self.outputs: dict[str, Grouped] = {}
        self.notes: list[str] = []
        self._trees: dict[str, Node] = {}

    # -- helpers ----------------------------------------------------------
    def tree(self, expression: str) -> Node:
        if expression not in self._trees:
            self._trees[expression] = parse_expression(expression)
        return self._trees[expression]

    def definition(self, instance: Instance) -> Transformation:
        transformation = self.mapping.transformation(instance.definition_name)
        if transformation is None:
            raise SimulationError(
                f"{self.mapping.name}: no definition for {instance.name!r}"
            )
        return transformation

    def group_of(self, instance_name: str, port_name: str) -> str:
        """Which output group a port belongs to; "" when there are none."""
        instance = self.mapping.instances.get(instance_name)
        if instance is None or instance.is_source:
            return ""
        transformation = self.mapping.transformation(instance.definition_name)
        if transformation is None or not transformation.output_groups:
            return ""
        port = transformation.port(port_name)
        if port is None or port.group is None:
            return ""
        if instance.kind is Kind.UNION:
            # A Union has one output group, so everything lands in "".
            return ""
        return port.group

    def feeds(self, instance: Instance) -> list[tuple[str, str, list[Connector]]]:
        """Incoming connectors, grouped by the upstream they come from."""
        by_origin: dict[tuple[str, str], list[Connector]] = {}
        for connector in self.graph.incoming.get(instance.name, ()):
            key = (
                connector.from_instance,
                self.group_of(connector.from_instance, connector.from_field),
            )
            by_origin.setdefault(key, []).append(connector)
        return [(inst, group, conns) for (inst, group), conns in by_origin.items()]

    def rows_from(
        self, instance_name: str, group: str, connectors: Sequence[Connector]
    ) -> list[dict[str, Any]]:
        available = self.outputs.get(instance_name)
        if available is None or group not in available:
            raise SimulationError(
                f"{self.mapping.name}: {instance_name}[{group or 'default'}] "
                "has not been run yet"
            )
        return [
            {c.to_field: row.get(c.from_field) for c in connectors}
            for row in available[group]
        ]

    def single_input(self, instance: Instance) -> list[dict[str, Any]]:
        feeds = self.feeds(instance)
        if len(feeds) != 1:
            raise SimulationError(
                f"{self.mapping.name}: {instance.name} has {len(feeds)} inputs, "
                "expected 1"
            )
        return self.rows_from(*feeds[0])

    # -- steps ------------------------------------------------------------
    def run_source(self, instance: Instance) -> Grouped:
        rows = self.sources.get(instance.definition_name)
        if rows is None:
            raise SimulationError(
                f"no data supplied for source {instance.definition_name!r}"
            )
        # No copy: every consumer goes through rows_from, which builds a
        # fresh projected dict per row, and nothing mutates these.
        return {"": list(rows)}

    def run_passthrough(self, instance: Instance) -> Grouped:
        """Source Qualifier: carries its input forward under the port names."""
        transformation = self.definition(instance)
        wanted = [p.name for p in transformation.ports if p.is_output]
        return {
            "": [
                {name: row.get(name) for name in wanted}
                for row in self.single_input(instance)
            ]
        }

    def run_expression(self, instance: Instance) -> Grouped:
        """Rows are processed in order, and variables persist between them.

        That persistence is the whole reason a variable can hold a running
        total, and the reason this cannot be done with a set-based query.
        """
        transformation = self.definition(instance)
        stateful = {p.name for p in stateful_variable_ports(transformation.ports)}

        declared = [p for p in transformation.ports if p.is_variable]
        if self.variable_order == VariableOrder.DEFERRED:
            # A variable whose whole expression is one port name is a
            # "remember this for the next row" slot. Updating those last is
            # what lets another variable read the previous row's value.
            remembering = [p for p in declared if _is_bare_reference(p)]
            variables = [p for p in declared if p not in remembering] + remembering
        else:
            variables = declared

        outputs = [
            p for p in transformation.ports if p.is_output and not p.is_variable
        ]

        # Variables keep their value from one row to the next, and start at
        # their datatype's default so the first row has something to read.
        carried: dict[str, Any] = {p.name: _initial_value(p) for p in variables}
        produced: list[dict[str, Any]] = []

        for row in self.single_input(instance):
            for port in variables:
                if not port.expression:
                    continue
                carried[port.name] = self.value(port, row, carried)
            produced.append(
                {port.name: self.value(port, row, carried) for port in outputs}
            )

        if stateful:
            self.notes.append(
                f"{instance.name}: {', '.join(sorted(stateful))} carried a value "
                f"between rows ({self.variable_order} order)"
            )
        return {"": produced}

    def value(self, port: Port, row: MappingABC[str, Any], variables) -> Any:
        if not port.expression:
            return row.get(port.name)
        try:
            return evaluate(
                self.tree(port.expression),
                row,
                variables=variables,
                parameters=self.parameters,
                result_datatype=port.datatype,
            )
        except EvalError as exc:
            raise SimulationError(f"{port.name}: {exc}") from exc

    def run_filter(self, instance: Instance) -> Grouped:
        transformation = self.definition(instance)
        condition = transformation.attribute("Filter Condition")
        rows = self.single_input(instance)
        if condition:
            tree = self.tree(condition)
            rows = [
                row
                for row in rows
                if evaluate(tree, row, parameters=self.parameters) is True
            ]
        wanted = [p.name for p in transformation.ports if p.is_output]
        return {"": [{name: row.get(name) for name in wanted} for row in rows]}

    def run_sorter(self, instance: Instance) -> Grouped:
        """Actually sorts, which is what makes the next step's order defined."""
        transformation = self.definition(instance)
        rows = self.single_input(instance)
        keys = [p.name for p in transformation.sort_keys]
        nulls_low = transformation.attribute("Null Treated Low", "NO") == "YES"

        if keys:

            def sort_key(row):
                out = []
                for key in keys:
                    value = row.get(key)
                    # Nulls sort together at one end; which end is a setting.
                    out.append(
                        (0 if nulls_low else 1)
                        if value is None
                        else (1 if nulls_low else 0)
                    )
                    out.append(_orderable(value))
                return tuple(out)

            rows = sorted(rows, key=sort_key)

            # Descending keys are applied one at a time, last key first, so
            # that mixed directions compose. Python's sort is stable, which is
            # what makes that work.
            for port in reversed(transformation.sort_keys):
                if (port.sort_direction or "").upper().startswith("DESC"):
                    rows = sorted(
                        rows,
                        key=lambda row, name=port.name: _orderable(row.get(name)),
                        reverse=True,
                    )

        wanted = [p.name for p in transformation.ports if p.is_output]
        return {"": [{name: row.get(name) for name in wanted} for row in rows]}

    def run_aggregator(self, instance: Instance) -> Grouped:
        transformation = self.definition(instance)
        rows = self.single_input(instance)

        group_ports = [p for p in transformation.ports if p.is_group_by]
        value_ports = [
            p
            for p in transformation.ports
            if p.is_output and not p.is_variable and not p.is_group_by
        ]

        groups: dict[tuple, list[dict]] = {}
        for row in rows:
            key = tuple(
                evaluate(self.tree(p.expression), row, parameters=self.parameters)
                if p.expression
                else row.get(p.name)
                for p in group_ports
            )
            groups.setdefault(key, []).append(row)

        produced = []
        for key, members in groups.items():
            out: dict[str, Any] = {
                port.name: value for port, value in zip(group_ports, key, strict=True)
            }
            for port in value_ports:
                out[port.name] = self.aggregate_port(port, members)
            produced.append(out)
        return {"": produced}

    def aggregate_port(self, port: Port, members: Sequence[dict]) -> Any:
        """An aggregate output, or the last row's value if it is not one."""
        if not port.expression:
            return members[-1].get(port.name)
        tree = self.tree(port.expression)
        if isinstance(tree, Call) and tree.name in AGGREGATES:
            if tree.name == "COUNT" and tree.arguments and _is_star(tree.arguments[0]):
                return len(members)
            inner = tree.arguments[0]
            condition = tree.arguments[1] if len(tree.arguments) > 1 else None
            values = []
            for row in members:
                if condition is not None and not (
                    evaluate(condition, row, parameters=self.parameters) is True
                ):
                    continue
                values.append(evaluate(inner, row, parameters=self.parameters))
            return aggregate(tree.name, values)
        # Not an aggregate: Informatica returns the last row of the group.
        return evaluate(
            tree, members[-1], parameters=self.parameters, result_datatype=port.datatype
        )

    def run_joiner(self, instance: Instance) -> Grouped:
        transformation = self.definition(instance)
        feeds = self.feeds(instance)
        if len(feeds) != 2:
            raise SimulationError(
                f"{self.mapping.name}: {instance.name} has {len(feeds)} inputs, "
                "expected 2"
            )

        master_rows: list[dict] | None = None
        detail_rows: list[dict] | None = None
        for origin, group, connectors in feeds:
            is_master = any(
                (transformation.port(c.to_field) or Port(name="")).is_master
                for c in connectors
            )
            rows = self.rows_from(origin, group, connectors)
            if is_master:
                master_rows = rows
            else:
                detail_rows = rows
        if master_rows is None or detail_rows is None:
            raise SimulationError(
                f"{self.mapping.name}: {instance.name} could not tell master "
                "from detail"
            )

        condition = transformation.attribute("Join Condition")
        if not condition:
            raise SimulationError(
                f"{self.mapping.name}: {instance.name} has no join condition"
            )
        pairs = _equality_pairs(self.tree(condition), transformation)
        join_type = transformation.attribute("Join Type", "Normal Join").lower()

        index: dict[tuple, list[dict]] = {}
        for row in master_rows:
            index.setdefault(
                tuple(row.get(master_port) for _, master_port in pairs), []
            ).append(row)

        wanted = [p.name for p in transformation.ports if p.is_output]
        produced: list[dict] = []
        matched_master: set[int] = set()

        for row in detail_rows:
            key = tuple(row.get(detail_port) for detail_port, _ in pairs)
            matches = index.get(key, [])
            for master in matches:
                matched_master.add(id(master))
                produced.append(_merge(master, row, wanted))
            if not matches and join_type in {"master outer join", "full outer join"}:
                # Keep the detail row; the master side comes out NULL.
                produced.append(_merge({}, row, wanted))

        if join_type in {"detail outer join", "full outer join"}:
            for master in master_rows:
                if id(master) not in matched_master:
                    produced.append(_merge(master, {}, wanted))

        # A normal join keeps only matches, which the loops above already do.

        return {"": produced}

    def run_router(self, instance: Instance) -> Grouped:
        transformation = self.definition(instance)
        rows = self.single_input(instance)
        produced: Grouped = {}

        for group in transformation.output_groups:
            ports = [
                p
                for p in transformation.ports
                if p.group == group.name and p.is_output
            ]
            if group.is_default:
                produced[group.name] = []
                continue
            tree = self.tree(group.expression) if group.expression else None
            selected = []
            for row in rows:
                if tree is not None and evaluate(
                    tree, row, parameters=self.parameters
                ) is not True:
                    continue
                selected.append(
                    {p.name: row.get(p.ref_field or p.name) for p in ports}
                )
            produced[group.name] = selected

        return produced

    def run_union(self, instance: Instance) -> Grouped:
        transformation = self.definition(instance)
        outputs = [
            p.name
            for p in transformation.ports
            if p.is_output
            and p.group in {g.name for g in transformation.output_groups}
        ]

        # Output port -> the input port feeding it, for each input leg.
        feeds_by_group: dict[str, dict[str, str]] = {}
        for dependency in transformation.field_dependencies:
            port = transformation.port(dependency.input_field)
            if port is None or port.group is None:
                continue
            feeds_by_group.setdefault(port.group, {})[dependency.output_field] = (
                dependency.input_field
            )

        produced: list[dict] = []
        for origin, group, connectors in self.feeds(instance):
            rows = self.rows_from(origin, group, connectors)
            leg = next(
                (
                    name
                    for name, mapped in feeds_by_group.items()
                    if any(c.to_field in mapped.values() for c in connectors)
                ),
                None,
            )
            if leg is None:
                continue
            mapped = feeds_by_group[leg]
            produced.extend(
                {out: row.get(mapped.get(out)) for out in outputs} for row in rows
            )
        return {"": produced}

    def run_target(self, instance: Instance) -> list[dict[str, Any]]:
        table = self.repo.targets.get(instance.definition_name)
        if table is None:
            raise SimulationError(f"unknown target {instance.definition_name!r}")
        rows = self.single_input(instance)
        names = [column.name for column in table.columns]
        return [{name: row.get(name) for name in names} for row in rows]

    # -- driver -----------------------------------------------------------
    def run(self) -> dict[str, list[dict]]:
        runners = {
            Kind.SOURCE_DEFINITION: self.run_source,
            Kind.SOURCE_QUALIFIER: self.run_passthrough,
            Kind.EXPRESSION: self.run_expression,
            Kind.FILTER: self.run_filter,
            Kind.SORTER: self.run_sorter,
            Kind.AGGREGATOR: self.run_aggregator,
            Kind.JOINER: self.run_joiner,
            Kind.ROUTER: self.run_router,
            Kind.UNION: self.run_union,
        }

        targets: dict[str, list[dict]] = {}
        order = self.graph.topological_order(self.mapping.instances)

        for name in order:
            instance = self.mapping.instances[name]
            if instance.is_target:
                targets[instance.definition_name] = self.run_target(instance)
                continue
            runner = runners.get(instance.kind)
            if runner is None:
                raise SimulationError(
                    f"{self.mapping.name}: cannot run {name} of type "
                    f"{instance.kind.value}"
                )
            self.outputs[name] = runner(instance)

        return targets


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _is_bare_reference(port: Port) -> bool:
    """Whether a variable is just one port name, e.g. ``v_PREV_ID = ID``."""
    from .expressions import Reference

    if not port.expression:
        return False
    try:
        return isinstance(parse_expression(port.expression), Reference)
    except Exception:
        return False


def _initial_value(port: Port) -> Any:
    """What a variable port holds before the first row.

    PowerCenter initialises variable ports from their datatype: zero for a
    number, an empty string for a string, NULL for a date. Without this, a
    running counter has nothing to add to on the first row.
    """
    if is_numeric(port.datatype):
        return 0
    if is_string(port.datatype):
        return ""
    return None


def _orderable(value: Any) -> Any:
    """A sort key that does not fall over on mixed types or None."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, datetime)):
        return value
    return str(value)


def _is_star(node: Node) -> bool:
    from .expressions import Star

    return isinstance(node, Star)


def _merge(master: MappingABC, detail: MappingABC, wanted: Iterable[str]) -> dict:
    return {
        name: detail[name] if name in detail else master.get(name)
        for name in wanted
    }


def _equality_pairs(
    tree: Node, transformation: Transformation
) -> list[tuple[str, str]]:
    """(detail port, master port) for each equality in the join condition.

    The condition is written with either side first, so each operand is placed
    by looking at whether its port is a master port.
    """
    from .expressions import Binary, Reference

    pairs: list[tuple[str, str]] = []

    def walk(node: Node) -> None:
        if not isinstance(node, Binary):
            return
        if node.operator in {"AND", "OR"}:
            walk(node.left)
            walk(node.right)
            return
        if node.operator != "=":
            return
        left, right = node.left, node.right
        if not (isinstance(left, Reference) and isinstance(right, Reference)):
            return
        left_port = transformation.port(left.name)
        right_port = transformation.port(right.name)
        if left_port is not None and left_port.is_master:
            pairs.append((right.name, left.name))
        elif right_port is not None and right_port.is_master:
            pairs.append((left.name, right.name))

    walk(tree)
    if not pairs:
        raise SimulationError(
            "could not work out which side of the join condition is the master"
        )
    return pairs


def run_repository(
    repo: Repository,
    sources: MappingABC[str, list[dict]],
    *,
    parameters: MappingABC[str, Any] | None = None,
    variable_order: str = VariableOrder.DOCUMENT,
) -> Result:
    """Run every mapping, feeding each one's targets to the next.

    Mappings run in the order the export declares them, except that a mapping
    reading a table another mapping writes must run second. The workflow says
    staging then core then enriched, and the table dependencies agree.
    """
    result = Result()
    available: dict[str, list[dict]] = {name: list(rows) for name, rows in sources.items()}

    if parameters is None:
        parameters = {
            variable.name: coerce(variable.default_value, variable.datatype)
            for mapping in repo.mappings
            for variable in mapping.variables
        }

    for mapping in _mapping_order(repo):
        run = _MappingRun(repo, mapping, available, parameters, variable_order)
        produced = run.run()
        result.notes.extend(run.notes)
        for table_name, rows in produced.items():
            result.tables[table_name] = rows
            table = repo.targets.get(table_name)
            result.columns[table_name] = (
                tuple(c.name for c in table.columns) if table else ()
            )
            # A target of one mapping is a source of the next.
            available[table_name] = rows

    return result


def _mapping_order(repo: Repository) -> list[Mapping]:
    """Order mappings so a mapping runs after whatever writes its sources."""
    writes = {
        mapping.name: {t.definition_name for t in mapping.target_instances}
        for mapping in repo.mappings
    }
    reads = {
        mapping.name: {i.definition_name for i in mapping.source_instances}
        for mapping in repo.mappings
    }

    remaining = {m.name: m for m in repo.mappings}
    ordered: list[Mapping] = []
    while remaining:
        ready = [
            name
            for name in remaining
            if not any(
                reads[name] & writes[other] for other in remaining if other != name
            )
        ]
        if not ready:
            raise SimulationError(
                "mappings read each other's targets in a cycle: "
                + ", ".join(sorted(remaining))
            )
        for name in sorted(ready):
            ordered.append(remaining.pop(name))
    return ordered
