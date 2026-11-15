"""Turn a model's chain of steps into one dbt model's SQL.

Each step becomes a CTE. The CTEs are emitted in dependency order and the model
ends with ``select * from final``, which is the shape dbt projects use.

The central idea is a lookup from "(step, port)" to "(CTE, column)". Every time
a CTE is emitted its output columns are registered there, so the next step can
ask where each of its inputs comes from. That is what makes column renames work
without matching names between steps: the answer always comes from the
connectors, never from the column happening to be called the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .expressions import (
    Binary,
    Call,
    Unary,
    ExpressionError,
    Number,
    Reference,
    order_variable_ports,
    parse_expression,
    stateful_variable_ports,
    translate,
)
from .graph import Graph, ModelPlan, Read, build_graph
from .naming import sql_type
from .model import Instance, Kind, Mapping, Port, Repository, Transformation

__all__ = ["SqlError", "build_model_sql", "INDENT"]

INDENT = "    "


class SqlError(Exception):
    """A step cannot be turned into SQL."""


@dataclass(frozen=True, slots=True)
class Column:
    """Where a column lives once a CTE has been emitted."""

    cte: str
    name: str


def _cte_name(instance_name: str) -> str:
    return instance_name.lower()


def _group_cte_name(instance_name: str, group_name: str) -> str:
    return f"{instance_name.lower()}__{group_name.lower()}"


def _select_item(expression: str, alias: str) -> str:
    """``expr as alias``, dropping the alias when it would be a no-op."""
    if expression == alias:
        return expression
    return f"{expression} as {alias}"


class _ModelBuilder:
    def __init__(
        self,
        repo: Repository,
        mapping: Mapping,
        graph: Graph,
        plan: ModelPlan,
    ) -> None:
        self.repo = repo
        self.mapping = mapping
        self.graph = graph
        self.plan = plan
        self.reads = {read.instance_name: read for read in plan.reads}
        # $$ variable datatypes, so a date variable is emitted quoted and cast.
        self.parameters = {v.name: v.datatype for v in mapping.variables}
        #: (instance name, port name) -> where that column ended up.
        self.columns: dict[tuple[str, str], Column] = {}
        self.ctes: list[tuple[str, str]] = []
        self.notes: list[str] = []

    # -- helpers ----------------------------------------------------------
    def definition(self, instance: Instance) -> Transformation:
        transformation = self.mapping.transformation(instance.definition_name)
        if transformation is None:
            raise SqlError(
                f"{self.mapping.name}: no definition for instance {instance.name!r}"
            )
        return transformation

    def add_cte(self, name: str, body: str) -> None:
        self.ctes.append((name, body))

    def register(self, instance_name: str, port_name: str, column: Column) -> None:
        self.columns[(instance_name, port_name)] = column

    def inputs_of(self, instance: Instance) -> dict[str, Column]:
        """This step's input port names, and where each one comes from."""
        found: dict[str, Column] = {}
        for connector in self.graph.incoming.get(instance.name, ()):
            key = (connector.from_instance, connector.from_field)
            origin = self.columns.get(key)
            if origin is None:
                raise SqlError(
                    f"{self.mapping.name}: {instance.name} reads "
                    f"{connector.from_instance}.{connector.from_field}, which "
                    "has not been emitted yet"
                )
            found[connector.to_field] = origin
        return found

    def upstream_ctes(self, inputs: dict[str, Column]) -> list[str]:
        seen: list[str] = []
        for column in inputs.values():
            if column.cte not in seen:
                seen.append(column.cte)
        return seen

    def resolver(
        self,
        inputs: dict[str, Column],
        *,
        qualify: bool,
        variables: dict[str, str] | None = None,
    ) -> Callable[[str], str]:
        """Turn a port name into the SQL that produces it."""
        variables = variables or {}

        def resolve(name: str) -> str:
            if name in variables:
                return variables[name]
            column = inputs.get(name)
            if column is None:
                raise SqlError(
                    f"{self.mapping.name}: nothing supplies {name!r}; "
                    f"available: {', '.join(sorted(inputs)) or 'nothing'}"
                )
            if qualify:
                return f"{column.cte}.{column.name}"
            return column.name

        return resolve

    def variable_sql(
        self,
        transformation: Transformation,
        inputs: dict[str, Column],
        qualify: bool,
    ) -> dict[str, str]:
        """Translate local variables, substituting each into the next.

        Variables are per-row scratch values. Inlining them keeps the model to
        one CTE per step; the cost is that a variable used by several outputs
        appears more than once in the SQL.
        """
        resolved: dict[str, str] = {}
        stateful = stateful_variable_ports(transformation.ports)
        remembered = _remembered_ports(transformation)
        for port in order_variable_ports(
            transformation.ports, allow_stateful=bool(stateful)
        ):
            if not port.expression:
                continue
            if port in stateful:
                resolved[port.name] = self.row_number_sql(
                    transformation, port, inputs, qualify
                )
                continue
            if port.name in remembered:
                resolved[port.name] = self.previous_row_sql(
                    transformation, port, inputs, qualify
                )
                continue
            sql = translate(
                port.expression,
                resolver=self.resolver(inputs, qualify=qualify, variables=resolved),
                result_datatype=port.datatype,
                parameters=self.parameters,
            )
            resolved[port.name] = f"({sql})"
        return resolved

    # -- stateful variables ----------------------------------------------
    def previous_row_sql(
        self,
        transformation: Transformation,
        port: Port,
        inputs: dict[str, Column],
        qualify: bool,
    ) -> str:
        """Translate a remember-the-previous-row variable into ``lag()``.

        ``v_PREV_CUSTOMER_ID = CUSTOMER_ID`` is a slot holding the previous
        row's value, which is how PowerCenter compares a row to the one
        before it. The ordering comes from the upstream Sorter, which is what
        makes "the previous row" mean anything.

        This is the same reading as the row-counter translation. Taking the
        declaration order literally instead would make the comparison test a
        row against itself, and the columns built on it would be constant.
        """
        ordering = self.sort_order_for(transformation)
        if not ordering:
            raise SqlError(
                f"{self.mapping.name}: {transformation.name}.{port.name} holds "
                "the previous row's value, but nothing upstream establishes a "
                "row order, so lag() has nothing to order by"
            )
        resolve = self.resolver(inputs, qualify=qualify)
        value = translate(
            port.expression, resolver=resolve, parameters=self.parameters
        )
        order_sql = ", ".join(resolve(key) for key in ordering)
        self.notes.append(
            f"{transformation.name}.{port.name} held the previous row's value; "
            "translated as lag()"
        )
        return f"lag({value}) over (order by {order_sql})"

    def row_number_sql(
        self,
        transformation: Transformation,
        port: Port,
        inputs: dict[str, Column],
        qualify: bool,
    ) -> str:
        """Translate a self-referencing counter into ``row_number()``.

        Recognises only the one shape PowerCenter uses for this::

            v_PREV_KEY = KEY
            v_COUNTER  = IIF(v_PREV_KEY = KEY, v_COUNTER + 1, 1)

        The partition keys come from the equality tests in the condition. The
        ordering comes from the nearest Sorter upstream, which is the one case
        where a Sorter is not droppable.
        """
        partition = self.counter_partition_keys(transformation, port)
        ordering = self.sort_order_for(transformation)
        if not ordering:
            raise SqlError(
                f"{self.mapping.name}: {transformation.name}.{port.name} is a "
                "running counter but nothing upstream establishes a row order, "
                "so it cannot become row_number()"
            )

        resolve = self.resolver(inputs, qualify=qualify)
        partition_sql = ", ".join(resolve(key) for key in partition)
        order_sql = ", ".join(resolve(key) for key in ordering if key not in partition)
        if not order_sql:
            order_sql = ", ".join(resolve(key) for key in ordering)

        self.notes.append(
            f"{transformation.name}.{port.name} was a variable holding its own "
            f"previous value; translated as row_number()"
        )
        over = f"partition by {partition_sql} order by {order_sql}"
        return f"row_number() over ({over})"

    def counter_partition_keys(
        self, transformation: Transformation, port: Port
    ) -> tuple[str, ...]:
        """Input columns the counter restarts on."""
        tree = parse_expression(port.expression or "")
        if not (isinstance(tree, Call) and tree.name == "IIF" and len(tree.arguments) == 3):
            raise SqlError(
                f"{self.mapping.name}: {transformation.name}.{port.name} holds "
                "its own previous value but is not the IIF(previous = current, "
                "counter + 1, 1) shape, so it cannot be translated"
            )

        previous = {
            p.name: p.expression
            for p in transformation.ports
            if p.is_variable and p.expression and isinstance(
                parse_expression(p.expression), Reference
            )
        }

        keys: list[str] = []
        for left, right in _equalities(tree.arguments[0]):
            for a, b in ((left, right), (right, left)):
                if isinstance(a, Reference) and a.name in previous and isinstance(
                    b, Reference
                ):
                    # v_PREV_X = X means "restart when X changes".
                    if previous[a.name].strip() == b.name and b.name not in keys:
                        keys.append(b.name)
        if not keys:
            raise SqlError(
                f"{self.mapping.name}: cannot work out what "
                f"{transformation.name}.{port.name} restarts on"
            )
        return tuple(keys)

    def sort_order_for(self, transformation: Transformation) -> tuple[str, ...]:
        """Sort keys of the nearest Sorter upstream of this step."""
        start = next(
            (
                instance.name
                for instance in self.plan.steps
                if instance.definition_name == transformation.name
            ),
            None,
        )
        if start is None:
            return ()

        queue = list(self.graph.feeders(start))
        seen = set(queue)
        while queue:
            name = queue.pop(0)
            instance = self.mapping.instances[name]
            if instance.kind is Kind.SORTER:
                sorter = self.definition(instance)
                if sorter.sort_keys:
                    return tuple(p.name for p in sorter.sort_keys)
            for feeder in self.graph.feeders(name):
                if feeder not in seen:
                    seen.add(feeder)
                    queue.append(feeder)
        return ()

    # -- step emitters ----------------------------------------------------
    def emit_source(self, instance: Instance) -> None:
        read = self.reads.get(instance.name)
        if read is None:
            raise SqlError(
                f"{self.mapping.name}: source instance {instance.name!r} has no table"
            )
        name = _cte_name(instance.name)

        # Columns are cast to the type the PowerCenter SOURCE declares rather
        # than left to whatever the warehouse infers, because the mapping's
        # behaviour depends on the declared type. RAW_SUPPLIES.PERISHABLE is a
        # varchar2 tested against 'Y'; read as a boolean instead, that
        # comparison quietly means something else.
        items = [
            f"cast({column.name.lower()} as {sql_type(column.datatype)}) "
            f"as {column.name.lower()}"
            for column in read.table.columns
        ]
        self.add_cte(name, _select(items, read.reference))
        for column in read.table.columns:
            self.register(instance.name, column.name, Column(name, column.name.lower()))

    def emit_passthrough(self, instance: Instance, note: str = "") -> None:
        """A step that only renames columns: Source Qualifier, Sorter."""
        inputs = self.inputs_of(instance)
        upstream = self.upstream_ctes(inputs)
        if len(upstream) != 1:
            raise SqlError(
                f"{self.mapping.name}: {instance.name} reads "
                f"{len(upstream)} inputs, expected 1"
            )
        transformation = self.definition(instance)
        name = _cte_name(instance.name)

        items = []
        for port in transformation.ports:
            if not port.is_output or port.name not in inputs:
                continue
            items.append(_select_item(inputs[port.name].name, port.name.lower()))
            self.register(instance.name, port.name, Column(name, port.name.lower()))

        if not items:
            raise SqlError(f"{self.mapping.name}: {instance.name} emits no columns")

        body = _select(items, upstream[0], note=note)
        self.add_cte(name, body)

    def emit_expression(self, instance: Instance) -> None:
        inputs = self.inputs_of(instance)
        upstream = self.upstream_ctes(inputs)
        if len(upstream) != 1:
            raise SqlError(
                f"{self.mapping.name}: {instance.name} reads "
                f"{len(upstream)} inputs, expected 1"
            )
        transformation = self.definition(instance)
        name = _cte_name(instance.name)
        variables = self.variable_sql(transformation, inputs, qualify=False)
        resolve = self.resolver(inputs, qualify=False, variables=variables)

        items = []
        for port in transformation.ports:
            if not port.is_output or port.is_variable:
                continue
            if port.expression and not port.is_passthrough:
                sql = translate(
                    port.expression,
                    resolver=resolve,
                    result_datatype=port.datatype,
                    parameters=self.parameters,
                )
            elif port.name in inputs:
                sql = inputs[port.name].name
            elif port.expression:
                sql = translate(
                    port.expression, resolver=resolve, parameters=self.parameters
                )
            else:
                continue
            items.append(_select_item(sql, port.name.lower()))
            self.register(instance.name, port.name, Column(name, port.name.lower()))

        self.add_cte(name, _select(items, upstream[0]))

    def emit_filter(self, instance: Instance) -> None:
        inputs = self.inputs_of(instance)
        upstream = self.upstream_ctes(inputs)
        transformation = self.definition(instance)
        name = _cte_name(instance.name)
        condition = transformation.attribute("Filter Condition")
        if not condition:
            self.emit_passthrough(instance, note="filter had no condition")
            return

        resolve = self.resolver(inputs, qualify=False)
        items = []
        for port in transformation.ports:
            if not port.is_output or port.name not in inputs:
                continue
            items.append(_select_item(inputs[port.name].name, port.name.lower()))
            self.register(instance.name, port.name, Column(name, port.name.lower()))

        where = translate(condition, resolver=resolve, parameters=self.parameters)
        self.add_cte(name, _select(items, upstream[0], where=where))

    def emit_aggregator(self, instance: Instance) -> None:
        inputs = self.inputs_of(instance)
        upstream = self.upstream_ctes(inputs)
        transformation = self.definition(instance)
        name = _cte_name(instance.name)
        resolve = self.resolver(inputs, qualify=False)

        grouped: list[str] = []
        aggregated: list[str] = []
        order: list[tuple[str, str]] = []

        for port in transformation.ports:
            if not port.is_output or port.is_variable:
                continue
            if port.is_group_by:
                sql = (
                    translate(
                        port.expression,
                        resolver=resolve,
                        parameters=self.parameters,
                    )
                    if port.expression
                    else resolve(port.name)
                )
                grouped.append(_select_item(sql, port.name.lower()))
                order.append(("group", port.name))
            elif port.expression:
                sql = translate(
                    port.expression,
                    resolver=resolve,
                    result_datatype=port.datatype,
                    parameters=self.parameters,
                )
                aggregated.append(_select_item(sql, port.name.lower()))
                order.append(("agg", port.name))
            else:
                continue
            self.register(instance.name, port.name, Column(name, port.name.lower()))

        items = grouped + aggregated
        group_by = (
            ", ".join(str(i + 1) for i in range(len(grouped))) if grouped else ""
        )
        self.add_cte(name, _select(items, upstream[0], group_by=group_by))

    def emit_joiner(self, instance: Instance) -> None:
        transformation = self.definition(instance)
        inputs = self.inputs_of(instance)
        name = _cte_name(instance.name)

        master_ctes: list[str] = []
        detail_ctes: list[str] = []
        for port_name, column in inputs.items():
            port = transformation.port(port_name)
            side = master_ctes if (port and port.is_master) else detail_ctes
            if column.cte not in side:
                side.append(column.cte)

        if len(master_ctes) != 1 or len(detail_ctes) != 1:
            raise SqlError(
                f"{self.mapping.name}: {instance.name} has master={master_ctes} "
                f"detail={detail_ctes}; expected one CTE on each side"
            )
        master, detail = master_ctes[0], detail_ctes[0]

        condition = transformation.attribute("Join Condition")
        if not condition:
            raise SqlError(f"{self.mapping.name}: {instance.name} has no join condition")
        on_sql = translate(
            condition,
            resolver=self.resolver(inputs, qualify=True),
            parameters=self.parameters,
        )

        join_type = transformation.attribute("Join Type", "Normal Join")
        left, right, keyword = _join_shape(join_type, master, detail)
        if keyword is None:
            raise SqlError(
                f"{self.mapping.name}: {instance.name} has unsupported join type "
                f"{join_type!r}"
            )

        items = []
        for port in transformation.ports:
            if not port.is_output or port.name not in inputs:
                continue
            column = inputs[port.name]
            items.append(
                _select_item(f"{column.cte}.{column.name}", port.name.lower())
            )
            self.register(instance.name, port.name, Column(name, port.name.lower()))

        body = _select(
            items,
            left,
            join=f"{keyword} {right} on {on_sql}",
            note=f"PowerCenter {join_type}: master={master}, detail={detail}",
        )
        self.add_cte(name, body)

    def emit_router(self, instance: Instance) -> None:
        transformation = self.definition(instance)
        inputs = self.inputs_of(instance)
        upstream = self.upstream_ctes(inputs)
        resolve = self.resolver(inputs, qualify=False)

        used_groups = {
            transformation.port(connector.from_field).group
            for connector in self.graph.outgoing.get(instance.name, ())
            if transformation.port(connector.from_field) is not None
        }

        for group in transformation.output_groups:
            if group.name not in used_groups:
                # Nothing consumes this branch; the default group is usually
                # left unconnected, and emitting it would be dead SQL.
                continue
            name = _group_cte_name(instance.name, group.name)
            items = []
            for port in transformation.ports:
                if port.group != group.name or not port.is_output:
                    continue
                origin = port.ref_field or port.name
                if origin not in inputs:
                    continue
                items.append(_select_item(inputs[origin].name, origin.lower()))
                self.register(instance.name, port.name, Column(name, origin.lower()))
            where = (
                translate(
                    group.expression, resolver=resolve, parameters=self.parameters
                )
                if group.expression
                else ""
            )
            self.add_cte(
                name,
                _select(items, upstream[0], where=where, note=f"router group {group.name}"),
            )

    def emit_union(self, instance: Instance) -> None:
        transformation = self.definition(instance)
        inputs = self.inputs_of(instance)
        name = _cte_name(instance.name)

        # Output port -> the input port feeding it, per input group.
        feeds: dict[str, dict[str, str]] = {}
        for dependency in transformation.field_dependencies:
            port = transformation.port(dependency.input_field)
            if port is None or port.group is None:
                continue
            feeds.setdefault(port.group, {})[dependency.output_field] = (
                dependency.input_field
            )

        outputs = [
            port.name
            for port in transformation.ports
            if port.is_output and port.group in {g.name for g in transformation.output_groups}
        ]
        if not outputs:
            raise SqlError(f"{self.mapping.name}: {instance.name} has no output ports")

        branches: list[str] = []
        for group in transformation.input_groups:
            mapped = feeds.get(group.name, {})
            leg_fields = [mapped.get(out) for out in outputs]
            if any(field is None or field not in inputs for field in leg_fields):
                continue
            cte = inputs[leg_fields[0]].cte
            items = [
                _select_item(inputs[field].name, out.lower())
                for out, field in zip(outputs, leg_fields)
            ]
            branches.append(_select(items, cte, note=f"union leg {group.name}"))

        if not branches:
            raise SqlError(
                f"{self.mapping.name}: {instance.name} has no connected input groups"
            )

        self.add_cte(name, "\n\n{}union all\n\n".format(INDENT).join(branches))
        for out in outputs:
            self.register(instance.name, out, Column(name, out.lower()))

    def emit_boundary(self, instance: Instance) -> None:
        """Read a step from its own model rather than rebuilding it here.

        The step is a fork feeding several targets, so it was promoted to an
        intermediate model. Its columns are whatever that model exposes.
        """
        model = self.plan.boundaries[instance.name]
        name = _cte_name(instance.name)
        self.add_cte(
            name,
            f"{INDENT}-- {instance.name} feeds several models, so it is built once\n"
            f"{INDENT}select * from {{{{ ref('{model}') }}}}",
        )
        transformation = self.mapping.transformation(instance.definition_name)
        ports = transformation.ports if transformation else ()
        for port in ports:
            if port.is_output and not port.is_variable:
                self.register(
                    instance.name, port.name, Column(name, port.name.lower())
                )

    def emit_final(self) -> str:
        """The last CTE: the columns this model hands onward."""
        leaf = self.plan.leaf

        if self.plan.is_intermediate:
            # The fork's own CTE already holds the right columns.
            origin = _cte_name(leaf.name)
            items = [
                _select_item(column, column) for column in self.plan.exposed
            ]
            return _select(items, origin)

        inputs = self.inputs_of(leaf)
        upstream = self.upstream_ctes(inputs)
        if len(upstream) != 1:
            raise SqlError(
                f"{self.mapping.name}: target {leaf.name} reads "
                f"{len(upstream)} inputs, expected 1"
            )

        items = []
        for column in self.plan.table.columns:
            origin = inputs.get(column.name)
            if origin is None:
                # A target column nothing writes to. Keep it, as NULL, so the
                # model's shape still matches the table it replaces.
                items.append(f"cast(null as varchar) as {column.name.lower()}")
                self.notes.append(f"target column {column.name} is never written")
                continue
            items.append(_select_item(origin.name, column.name.lower()))

        return _select(items, upstream[0])

    # -- driver -----------------------------------------------------------
    def build(self) -> str:
        emitters = {
            Kind.SOURCE_DEFINITION: self.emit_source,
            Kind.SOURCE_QUALIFIER: self.emit_passthrough,
            Kind.EXPRESSION: self.emit_expression,
            Kind.FILTER: self.emit_filter,
            Kind.AGGREGATOR: self.emit_aggregator,
            Kind.JOINER: self.emit_joiner,
            Kind.ROUTER: self.emit_router,
            Kind.UNION: self.emit_union,
        }

        for instance in self.plan.steps:
            if instance.name in self.plan.boundaries:
                self.emit_boundary(instance)
                continue
            if instance.kind is Kind.SORTER:
                # Row order does not survive into a table, so the sort itself
                # is dropped. Where it matters, row_number() reads the keys
                # straight off the Sorter instead.
                self.emit_passthrough(instance, note="sort order dropped")
                continue
            emitter = emitters.get(instance.kind)
            if emitter is None:
                raise SqlError(
                    f"{self.mapping.name}: cannot convert {instance.name} "
                    f"of type {instance.kind.value}"
                )
            emitter(instance)

        self.add_cte("final", self.emit_final())
        return self.render()

    def render(self) -> str:
        header = [f"-- Generated from PowerCenter mapping {self.mapping.name}"]
        if self.plan.is_intermediate:
            header.append(
                f"-- shared step {self.plan.leaf.name}, built once and "
                "referenced by several models"
            )
        else:
            header.append(
                f"-- target {self.plan.leaf.definition_name} "
                f"({self.plan.schema}.{self.plan.table.name})"
            )
        for note in dict.fromkeys(self.notes):
            header.append(f"-- note: {note}")

        blocks = [f"{name} as (\n{body}\n)" for name, body in self.ctes]
        return (
            "\n".join(header)
            + "\n\nwith\n\n"
            + ",\n\n".join(blocks)
            + "\n\nselect * from final\n"
        )


def _remembered_ports(transformation: Transformation) -> frozenset[str]:
    """Variables that exist to hold the previous row's value.

    A variable whose whole expression is one port name, and which another
    variable reads. Without a reader it is only an alias, so it is left alone.
    """
    bare = {
        port.name
        for port in transformation.ports
        if port.is_variable
        and port.expression
        and isinstance(parse_expression(port.expression), Reference)
    }
    if not bare:
        return frozenset()

    read_by_others: set[str] = set()
    for port in transformation.ports:
        if not (port.is_variable and port.expression) or port.name in bare:
            continue
        read_by_others |= _names_of(parse_expression(port.expression)) & bare
    return frozenset(read_by_others)


def _names_of(node) -> set[str]:
    if isinstance(node, Reference):
        return {node.name}
    if isinstance(node, Unary):
        return _names_of(node.operand)
    if isinstance(node, Binary):
        return _names_of(node.left) | _names_of(node.right)
    if isinstance(node, Call):
        names: set[str] = set()
        for argument in node.arguments:
            names |= _names_of(argument)
        return names
    return set()


def _equalities(node) -> Iterable[tuple[object, object]]:
    """Every ``a = b`` inside a chain of ANDs."""
    if isinstance(node, Binary):
        if node.operator == "=":
            yield node.left, node.right
        elif node.operator in {"AND", "OR"}:
            yield from _equalities(node.left)
            yield from _equalities(node.right)


def _join_shape(
    join_type: str, master: str, detail: str
) -> tuple[str, str, str | None]:
    """Which table leads, and which SQL join to use.

    PowerCenter names these from the side that may be dropped, which is the
    opposite of how SQL reads. A Master Outer Join keeps every *detail* row,
    so in SQL the detail table leads and the master is joined to it.
    """
    normalised = join_type.strip().lower()
    if normalised == "normal join":
        return detail, master, "inner join"
    if normalised == "master outer join":
        return detail, master, "left join"
    if normalised == "detail outer join":
        return master, detail, "left join"
    if normalised == "full outer join":
        return detail, master, "full outer join"
    return detail, master, None


def _select(
    items: list[str],
    source: str,
    *,
    where: str = "",
    join: str = "",
    group_by: str = "",
    note: str = "",
) -> str:
    lines: list[str] = []
    if note:
        lines.append(f"{INDENT}-- {note}")
    lines.append(f"{INDENT}select")
    lines.extend(f"{INDENT}{INDENT}{item}," for item in items[:-1])
    lines.append(f"{INDENT}{INDENT}{items[-1]}")
    lines.append(f"{INDENT}from {source}")
    if join:
        lines.append(f"{INDENT}{join}")
    if where:
        lines.append(f"{INDENT}where {where}")
    if group_by:
        lines.append(f"{INDENT}group by {group_by}")
    return "\n".join(lines)


def build_model_sql(repo: Repository, plan: ModelPlan) -> str:
    """Generate the SQL for one dbt model."""
    mapping = repo.mapping(plan.mapping_name)
    if mapping is None:
        raise SqlError(f"no mapping called {plan.mapping_name!r}")
    builder = _ModelBuilder(repo, mapping, build_graph(mapping), plan)
    try:
        return builder.build()
    except ExpressionError as exc:
        raise SqlError(f"{plan.name}: {exc}") from exc
