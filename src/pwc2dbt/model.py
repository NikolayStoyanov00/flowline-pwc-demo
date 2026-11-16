"""PowerCenter concepts as plain Python objects.

No XML, no SQL. See ``docs/concepts.md`` for how these map onto SQL and dbt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from collections.abc import Iterable, Mapping as MappingABC

EMPTY_ATTRS: MappingABC[str, str] = MappingProxyType({})


class CycleError(Exception):
    """A dependency graph has a cycle, so it cannot be ordered."""

    def __init__(self, names: Iterable[str]) -> None:
        self.names = tuple(sorted(names))
        super().__init__(", ".join(self.names))


def dependency_order(dependencies: MappingABC[str, set[str]]) -> tuple[str, ...]:
    """Order names so each comes after everything it depends on.

    Ties break alphabetically, which is what makes generated SQL identical
    from run to run. Raises :class:`CycleError` naming the names involved.
    """
    remaining = {name: set(deps) for name, deps in dependencies.items()}
    ordered: list[str] = []
    while remaining:
        ready = sorted(name for name, deps in remaining.items() if not deps)
        if not ready:
            raise CycleError(remaining)
        for name in ready:
            ordered.append(name)
            del remaining[name]
        for deps in remaining.values():
            deps.difference_update(ready)
    return tuple(ordered)

NOT_A_KEY = "NOT A KEY"


class Kind(str, Enum):
    """What a transformation does, i.e. which SQL clause it becomes."""

    SOURCE_DEFINITION = "Source Definition"
    SOURCE_QUALIFIER = "Source Qualifier"
    EXPRESSION = "Expression"
    FILTER = "Filter"
    JOINER = "Joiner"
    SORTER = "Sorter"
    AGGREGATOR = "Aggregator"
    ROUTER = "Router"
    UNION = "Union"
    TARGET_DEFINITION = "Target Definition"
    UNKNOWN = "Unknown"

    @classmethod
    def classify(cls, type_attr: str, template_name: str | None = None) -> Kind:
        """Normalise a raw ``TYPE`` attribute.

        A Union is exported as ``TYPE="Custom Transformation"`` and is only
        identifiable by its ``TEMPLATENAME``. Any other custom transformation
        is UNKNOWN rather than Union: guessing would emit silently wrong SQL.
        """
        if type_attr == "Custom Transformation":
            if (template_name or "").strip() == "Union Transformation":
                return cls.UNION
            return cls.UNKNOWN
        try:
            return cls(type_attr)
        except ValueError:
            return cls.UNKNOWN


@dataclass(frozen=True, slots=True)
class Column:
    """A column of a physical table."""

    name: str
    datatype: str = ""
    precision: int = 0
    scale: int = 0
    nullable: bool = True
    key_type: str = NOT_A_KEY
    field_number: int = 0
    referenced_table: str | None = None
    referenced_field: str | None = None

    @property
    def is_primary_key(self) -> bool:
        return "PRIMARY KEY" in self.key_type

    @property
    def is_foreign_key(self) -> bool:
        return "FOREIGN KEY" in self.key_type


@dataclass(frozen=True, slots=True)
class Table:
    """A physical table: a ``SOURCE`` read from, or a ``TARGET`` written to.

    Each target becomes one dbt model.
    """

    name: str
    columns: tuple[Column, ...] = ()
    database_type: str = ""
    connection_name: str = ""
    schema: str = ""

    @property
    def primary_keys(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.is_primary_key)

    @property
    def foreign_keys(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.is_foreign_key)


@dataclass(frozen=True, slots=True)
class Port:
    """A column of an intermediate step, plus the expression computing it.

    Think of a single item in a SELECT list: ``PRICE / 100 AS PRODUCT_PRICE``
    is a port named ``PRODUCT_PRICE`` whose expression is ``PRICE / 100``.
    """

    name: str
    datatype: str = ""
    precision: int = 0
    scale: int = 0
    port_type: str = "INPUT"
    expression: str | None = None
    expression_type: str | None = None
    group: str | None = None
    ref_field: str | None = None
    is_sort_key: bool = False
    sort_direction: str | None = None

    @property
    def is_input(self) -> bool:
        return "INPUT" in self.port_type

    @property
    def is_output(self) -> bool:
        return "OUTPUT" in self.port_type

    @property
    def is_master(self) -> bool:
        """Whether this is the master side of a join.

        This flag is the only record of master vs detail, which decides the
        join direction.
        """
        return "MASTER" in self.port_type

    @property
    def is_variable(self) -> bool:
        """A scratch value reused by other ports in the same transformation."""
        return self.port_type == "LOCAL VARIABLE"

    @property
    def is_group_by(self) -> bool:
        return self.expression_type == "GROUPBY"

    @property
    def is_passthrough(self) -> bool:
        """Whether the expression just re-emits the port's own input column."""
        return self.expression is not None and self.expression.strip() == self.name


@dataclass(frozen=True, slots=True)
class Group:
    """A named branch of a Router (one ``WHERE``) or leg of a Union."""

    name: str
    type: str
    order: int = 0
    expression: str | None = None

    @property
    def is_input(self) -> bool:
        return self.type.startswith("INPUT")

    @property
    def is_output(self) -> bool:
        return self.type.startswith("OUTPUT")

    @property
    def is_default(self) -> bool:
        """The Router branch taking rows that matched no other branch."""
        return "DEFAULT" in self.type


@dataclass(frozen=True, slots=True)
class FieldDependency:
    """Which input port of a Union leg lines up with which output column."""

    input_field: str
    output_field: str


@dataclass(frozen=True, slots=True)
class Transformation:
    """One step of a pipeline; becomes one CTE.

    Defined inside a mapping, so names are unique only within that mapping.
    """

    name: str
    kind: Kind
    raw_type: str = ""
    ports: tuple[Port, ...] = ()
    attributes: MappingABC[str, str] = field(default_factory=lambda: EMPTY_ATTRS)
    groups: tuple[Group, ...] = ()
    field_dependencies: tuple[FieldDependency, ...] = ()

    def port(self, name: str) -> Port | None:
        for port in self.ports:
            if port.name == name:
                return port
        return None

    def attribute(self, name: str, default: str = "") -> str:
        return self.attributes.get(name, default)

    @property
    def output_groups(self) -> tuple[Group, ...]:
        return tuple(g for g in self.groups if g.is_output)

    @property
    def input_groups(self) -> tuple[Group, ...]:
        return tuple(g for g in self.groups if g.is_input)

    @property
    def sort_keys(self) -> tuple[Port, ...]:
        return tuple(p for p in self.ports if p.is_sort_key)

    @property
    def output_ports(self) -> tuple[Port, ...]:
        """Ports this step hands to whatever reads it."""
        return tuple(p for p in self.ports if p.is_output)

    @property
    def value_ports(self) -> tuple[Port, ...]:
        """Output ports that are real columns, so not scratch variables."""
        return tuple(p for p in self.ports if p.is_output and not p.is_variable)

    @property
    def input_feeds(self) -> dict[str, dict[str, str]]:
        """Union legs: input group -> {output port: the port feeding it}.

        Inverts ``FIELDDEPENDENCY``, which is how a Union says which leg's
        column lines up with which output column.
        """
        feeds: dict[str, dict[str, str]] = {}
        for dependency in self.field_dependencies:
            port = self.port(dependency.input_field)
            if port is None or port.group is None:
                continue
            feeds.setdefault(port.group, {})[dependency.output_field] = (
                dependency.input_field
            )
        return feeds


@dataclass(frozen=True, slots=True)
class Instance:
    """One *use* of a table or transformation in a pipeline: a SQL alias.

    ``name`` is the alias and is what connectors reference; ``definition_name``
    is the thing being aliased. Reading one table twice gives two instances
    (``STG_ORDERS``, ``STG_ORDERS1``) over a single definition, exactly like
    ``from orders o1, orders o2``.
    """

    name: str
    definition_name: str
    kind: Kind
    instance_type: str = "TRANSFORMATION"
    connection_name: str = ""
    associated_sources: tuple[str, ...] = ()
    attributes: MappingABC[str, str] = field(default_factory=lambda: EMPTY_ATTRS)

    @property
    def is_source(self) -> bool:
        return self.instance_type == "SOURCE"

    @property
    def is_target(self) -> bool:
        return self.instance_type == "TARGET"


@dataclass(frozen=True, slots=True)
class Connector:
    """A single column flowing from one step to the next: ``x.a AS b``.

    There is one per column pair, so a step with ten columns has ten of them.
    Column renames happen here, so the wires must be followed rather than
    matching names between steps.
    """

    from_instance: str
    from_field: str
    to_instance: str
    to_field: str
    from_instance_type: str = ""
    to_instance_type: str = ""


@dataclass(frozen=True, slots=True)
class MappingVariable:
    """A ``$$`` parameter supplied at run time; becomes a dbt ``var()``."""

    name: str
    datatype: str = ""
    default_value: str = ""


@dataclass(frozen=True, slots=True)
class Mapping:
    """A whole ETL job: steps (``instances``) wired by columns (``connectors``).

    Holds several independent pipelines, one per target, so a mapping usually
    produces several dbt models rather than one.
    """

    name: str
    transformations: MappingABC[str, Transformation] = field(
        default_factory=lambda: MappingProxyType({})
    )
    instances: MappingABC[str, Instance] = field(
        default_factory=lambda: MappingProxyType({})
    )
    connectors: tuple[Connector, ...] = ()
    variables: tuple[MappingVariable, ...] = ()
    target_load_order: MappingABC[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def target_instances(self) -> tuple[Instance, ...]:
        """The tables this job writes; one dbt model each."""
        targets = [i for i in self.instances.values() if i.is_target]
        return tuple(
            sorted(
                targets,
                key=lambda i: (self.target_load_order.get(i.name, 0), i.name),
            )
        )

    @property
    def source_instances(self) -> tuple[Instance, ...]:
        return tuple(
            sorted(
                (i for i in self.instances.values() if i.is_source),
                key=lambda i: i.name,
            )
        )

    def transformation(self, name: str) -> Transformation | None:
        return self.transformations.get(name)


@dataclass(frozen=True, slots=True)
class Repository:
    """The whole export: every table definition and every job in one folder."""

    name: str
    folder_name: str
    database_type: str = ""
    sources: MappingABC[str, Table] = field(default_factory=lambda: MappingProxyType({}))
    targets: MappingABC[str, Table] = field(default_factory=lambda: MappingProxyType({}))
    mappings: tuple[Mapping, ...] = ()

    def mapping(self, name: str) -> Mapping | None:
        for mapping in self.mappings:
            if mapping.name == name:
                return mapping
        return None
