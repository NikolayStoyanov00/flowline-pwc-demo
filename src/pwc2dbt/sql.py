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
from collections.abc import Callable, Iterable

from .expressions import (
    Binary,
    Call,
    ExpressionError,
    Number,
    Reference,
    VariableOrder,
    order_variable_ports,
    parse_expression,
    references,
    stateful_variable_ports,
    translate,
)
from .graph import Graph, ModelPlan, build_graph
from .model import Instance, Kind, Mapping, Port, Repository, Transformation
from .naming import sql_type

__all__ = ["SqlError", "build_model_sql"]

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
        mapping: Mapping,
        graph: Graph,
        plan: ModelPlan,
        variable_order: str = VariableOrder.DOCUMENT,
    ) -> None:
        self.mapping = mapping
        self.graph = graph
        self.plan = plan
        self.variable_order = variable_order
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

    def sole_upstream(self, instance: Instance, inputs: dict[str, Column]) -> str:
        """The one CTE a step reads, or an error naming the step.

        Most steps take a single input. A step wired from two upstreams would
        otherwise select from the first while emitting the second's columns
        unqualified.
        """
        upstream = self.upstream_ctes(inputs)
        if len(upstream) != 1:
            raise SqlError(
                f"{self.mapping.name}: {instance.name} reads "
                f"{len(upstream)} inputs, expected 1"
            )
        return upstream[0]

    def column_item(
        self,
        instance: Instance,
        cte: str,
        port_name: str,
        sql: str,
        alias: str | None = None,
    ) -> str:
        """One select item, registering where the next step will find it.

        Emitting and registering belong together: the alias written into the
        SQL has to be the name the lookup records, or a later step asks for a
        column that is not there.
        """
        column = (alias or port_name).lower()
        self.register(instance.name, port_name, Column(cte, column))
        return _select_item(sql, column)

    def carry(
        self,
        instance: Instance,
        transformation: Transformation,
        inputs: dict[str, Column],
        cte: str,
        *,
        qualify: bool = False,
    ) -> list[str]:
        """Select items for every output port that an input feeds unchanged.

        Steps that only rename — Source Qualifier, Sorter, Filter — consist of
        nothing else, and a Joiner's output is this plus a join clause.
        """
        items = []
        for port in transformation.ports:
            if not port.is_output or port.name not in inputs:
                continue
            column = inputs[port.name]
            sql = f"{column.cte}.{column.name}" if qualify else column.name
            items.append(self.column_item(instance, cte, port.name, sql))
        return items

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
        remembered = (
            _remembered_ports(transformation)
            if self.variable_order == VariableOrder.DEFERRED
            else frozenset()
        )
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
        order_sql = _order_by(ordering, resolve)
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
        remaining = [k for k in ordering if k[0] not in partition] or list(ordering)
        order_sql = _order_by(remaining, resolve)

        self.notes.append(
            f"{transformation.name}.{port.name} was a variable holding its own "
            f"previous value; translated as row_number()"
        )
        over = f"order by {order_sql}"
        if partition_sql:
            over = f"partition by {partition_sql} {over}"
        return f"row_number() over ({over})"

    def counter_partition_keys(
        self, transformation: Transformation, port: Port
    ) -> tuple[str, ...]:
        """Input columns the counter restarts on.

        Only the counter shape is accepted — ``+ 1`` and a reset to ``1``.
        Checking the condition alone is not enough: a running *total*
        (``IIF(v_PREV_C = C, v_TOTAL + AMOUNT, AMOUNT)``) has the same
        condition and also reads its own previous value, and emitting
        ``row_number()`` for it would silently replace every total with a row
        ordinal. A running total is a window ``sum``, which is not translated.
        """
        tree = parse_expression(port.expression or "")
        if not _is_increment_by_one(tree, port.name):
            raise SqlError(
                f"{self.mapping.name}: {transformation.name}.{port.name} reads "
                "its own previous value but is not the "
                "IIF(previous = current, counter + 1, 1) shape, so it cannot "
                "be translated; only a running count is supported"
            )

        previous = _bare_reference_ports(transformation)

        if self.variable_order == VariableOrder.DOCUMENT:
            # v_PREV_X was assigned X earlier in this same row, so
            # "v_PREV_X = X" compares the row to itself and is always true.
            # The counter therefore never restarts.
            self.notes.append(
                f"{transformation.name}.{port.name} restarts on nothing: "
                f"{', '.join(sorted(previous))} "
                "is assigned before the test that reads it, so the test is "
                "always true (see README quirk 3)"
            )
            return ()

        keys: list[str] = []
        for left, right in _equalities(tree.arguments[0]):
            for a, b in ((left, right), (right, left)):
                if not (isinstance(a, Reference) and isinstance(b, Reference)):
                    continue
                # v_PREV_X = X means "restart when X changes".
                if previous.get(a.name) == b.name and b.name not in keys:
                    keys.append(b.name)
        if not keys:
            raise SqlError(
                f"{self.mapping.name}: cannot work out what "
                f"{transformation.name}.{port.name} restarts on"
            )
        return tuple(keys)

    def sort_order_for(
        self, transformation: Transformation
    ) -> tuple[tuple[str, str], ...]:
        """Sort keys of the nearest Sorter upstream, with their directions.

        The direction is carried because a window function has to order the
        same way the Sorter did: a DESCENDING key makes a counter number rows
        from the other end, and makes lag() return the following row.
        """
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
                    return tuple(
                        (
                            p.name,
                            "desc"
                            if (p.sort_direction or "").upper().startswith("DESC")
                            else "asc",
                        )
                        for p in sorter.sort_keys
                    )
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
        upstream = self.sole_upstream(instance, inputs)
        transformation = self.definition(instance)
        name = _cte_name(instance.name)

        items = self.carry(instance, transformation, inputs, name)
        if not items:
            raise SqlError(f"{self.mapping.name}: {instance.name} emits no columns")

        self.add_cte(name, _select(items, upstream, note=note))

    def emit_expression(self, instance: Instance) -> None:
        inputs = self.inputs_of(instance)
        upstream = self.sole_upstream(instance, inputs)
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
            items.append(self.column_item(instance, name, port.name, sql))

        self.add_cte(name, _select(items, upstream))

    def emit_filter(self, instance: Instance) -> None:
        inputs = self.inputs_of(instance)
        transformation = self.definition(instance)
        name = _cte_name(instance.name)
        condition = transformation.attribute("Filter Condition")
        if not condition:
            self.emit_passthrough(instance, note="filter had no condition")
            return

        upstream = self.sole_upstream(instance, inputs)
        resolve = self.resolver(inputs, qualify=False)
        items = self.carry(instance, transformation, inputs, name)
        where = translate(condition, resolver=resolve, parameters=self.parameters)
        self.add_cte(name, _select(items, upstream, where=where))

    def emit_aggregator(self, instance: Instance) -> None:
        inputs = self.inputs_of(instance)
        upstream = self.sole_upstream(instance, inputs)
        transformation = self.definition(instance)
        name = _cte_name(instance.name)
        resolve = self.resolver(inputs, qualify=False)

        grouped: list[str] = []
        aggregated: list[str] = []
        #: Group keys the transformation does not emit. They still have to
        #: reach the GROUP BY, or the aggregation runs at a coarser grain and
        #: every aggregate silently covers too many rows.
        grouped_not_emitted: list[str] = []

        for port in transformation.ports:
            if port.is_variable:
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
                if not port.is_output:
                    grouped_not_emitted.append(sql)
                    continue
                grouped.append(self.column_item(instance, name, port.name, sql))
            elif not port.is_output:
                continue
            elif port.expression:
                sql = translate(
                    port.expression,
                    resolver=resolve,
                    result_datatype=port.datatype,
                    parameters=self.parameters,
                )
                aggregated.append(self.column_item(instance, name, port.name, sql))

        items = grouped + aggregated
        # Emitted keys go in by position, which is the dbt house style; a key
        # that is not selected has to go in as its own expression.
        keys = [str(i + 1) for i in range(len(grouped))] + grouped_not_emitted
        self.add_cte(name, _select(items, upstream, group_by=", ".join(keys)))

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

        items = self.carry(instance, transformation, inputs, name, qualify=True)
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
        upstream = self.sole_upstream(instance, inputs)
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
                items.append(
                    self.column_item(
                        instance, name, port.name, inputs[origin].name, alias=origin
                    )
                )
            if group.is_default:
                where = self.default_group_condition(transformation, resolve)
            elif group.expression:
                where = translate(
                    group.expression, resolver=resolve, parameters=self.parameters
                )
            else:
                where = ""
            self.add_cte(
                name,
                _select(items, upstream, where=where, note=f"router group {group.name}"),
            )

    def default_group_condition(
        self, transformation: Transformation, resolve: Callable[[str], str]
    ) -> str:
        """The rows a Router's default group takes: those no other group took.

        A default group has no condition of its own, so emitting it with no
        ``where`` would send it every row. A condition evaluating to NULL
        counts as not matched, hence the ``coalesce``.
        """
        others = [g for g in transformation.output_groups if not g.is_default]
        if any(not g.expression for g in others):
            # A group with no condition takes every row, leaving none over.
            return "false"
        return " and ".join(
            "not coalesce("
            + translate(g.expression, resolver=resolve, parameters=self.parameters)
            + ", false)"
            for g in others
        )

    def emit_union(self, instance: Instance) -> None:
        transformation = self.definition(instance)
        inputs = self.inputs_of(instance)
        name = _cte_name(instance.name)

        feeds = transformation.input_feeds

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
                for out, field in zip(outputs, leg_fields, strict=True)
            ]
            branches.append(_select(items, cte, note=f"union leg {group.name}"))

        if not branches:
            raise SqlError(
                f"{self.mapping.name}: {instance.name} has no connected input groups"
            )

        self.add_cte(name, f"\n\n{INDENT}union all\n\n".join(branches))
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
        upstream = self.sole_upstream(leaf, inputs)

        items = []
        for column in self.plan.table.columns:
            origin = inputs.get(column.name)
            if origin is None:
                # A target column nothing writes to. Keep it, as NULL, so the
                # model's shape still matches the table it replaces, including
                # its type: an untyped NULL column would be a varchar.
                items.append(
                    f"cast(null as {sql_type(column.datatype)}) "
                    f"as {column.name.lower()}"
                )
                self.notes.append(f"target column {column.name} is never written")
                continue
            items.append(_select_item(origin.name, column.name.lower()))

        return _select(items, upstream)

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
        body = ",\n\n".join(f"{name} as (\n{sql}\n)" for name, sql in self.ctes)
        for note in dict.fromkeys(self.notes):
            # The header is the audit trail for the conversion, so drop a note
            # about a translation that ended up discarded — a remembered
            # variable becomes lag() only to be replaced when the counter that
            # reads it becomes row_number().
            if "lag()" in note and "lag(" not in body:
                continue
            header.append(f"-- note: {note}")

        return (
            "\n".join(header)
            + "\n\nwith\n\n"
            + body
            + "\n\nselect * from final\n"
        )


def _order_by(
    keys: Iterable[tuple[str, str]], resolve: Callable[[str], str]
) -> str:
    """An ORDER BY list, keeping each key's direction. `asc` is left implicit."""
    return ", ".join(
        resolve(name) if direction == "asc" else f"{resolve(name)} desc"
        for name, direction in keys
    )


def _is_increment_by_one(tree, port_name: str) -> bool:
    """Whether ``tree`` is ``IIF(<cond>, <port> + 1, 1)``.

    The branches are what separate a running count from a running total; the
    condition is identical in both.
    """
    if not (isinstance(tree, Call) and tree.name == "IIF" and len(tree.arguments) == 3):
        return False
    increment, reset = tree.arguments[1], tree.arguments[2]
    if not (isinstance(reset, Number) and reset.text.strip() in {"1", "1.0"}):
        return False
    if not (isinstance(increment, Binary) and increment.operator == "+"):
        return False
    # Either operand order: v_C + 1 or 1 + v_C.
    for own, one in (
        (increment.left, increment.right),
        (increment.right, increment.left),
    ):
        if (
            isinstance(own, Reference)
            and own.name == port_name
            and isinstance(one, Number)
            and one.text.strip() in {"1", "1.0"}
        ):
            return True
    return False


def _bare_reference_ports(transformation: Transformation) -> dict[str, str]:
    """Variable ports that are one port name, mapped to the name they read.

    ``v_PREV_CUSTOMER_ID = CUSTOMER_ID`` gives
    ``{"v_PREV_CUSTOMER_ID": "CUSTOMER_ID"}``. Defined once because two
    translations depend on agreeing about it: which variables become ``lag()``,
    and what a counter restarts on.
    """
    found: dict[str, str] = {}
    for port in transformation.ports:
        if not (port.is_variable and port.expression):
            continue
        tree = parse_expression(port.expression)
        if isinstance(tree, Reference):
            found[port.name] = tree.name
    return found


def _remembered_ports(transformation: Transformation) -> frozenset[str]:
    """Variables that exist to hold the previous row's value.

    A variable whose whole expression is one port name, and which another
    variable reads. Without a reader it is only an alias, so it is left alone.
    """
    bare = frozenset(_bare_reference_ports(transformation))
    if not bare:
        return frozenset()

    read_by_others: set[str] = set()
    for port in transformation.ports:
        if not (port.is_variable and port.expression) or port.name in bare:
            continue
        read_by_others |= references(parse_expression(port.expression)) & bare
    return frozenset(read_by_others)


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
    if not items:
        raise SqlError(f"nothing to select from {source}")
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


def build_model_sql(
    repo: Repository,
    plan: ModelPlan,
    variable_order: str = VariableOrder.DOCUMENT,
) -> str:
    """Generate the SQL for one dbt model."""
    mapping = repo.mapping(plan.mapping_name)
    if mapping is None:
        raise SqlError(f"no mapping called {plan.mapping_name!r}")
    builder = _ModelBuilder(mapping, build_graph(mapping), plan, variable_order)
    try:
        return builder.build()
    except ExpressionError as exc:
        raise SqlError(f"{plan.name}: {exc}") from exc
