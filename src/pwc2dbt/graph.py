"""Turn each mapping into a graph, then work out which dbt models it produces.

A mapping is a directed graph: instances are nodes, connectors are edges. Each
target is a leaf, and one dbt model is built per target by walking backwards
from it.

Splitting on *connected components* would be wrong. The enriched mapping is a
single connected graph that writes five different targets, because the five
pipelines share upstream steps.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping as MappingABC

from .model import Connector, Instance, Kind, Mapping, Repository, Table
from .naming import layer_for_schema, model_name, schema_from_pre_sql, source_name

__all__ = ["Graph", "GraphError", "ModelPlan", "Read", "build_graph", "plan_models"]


class GraphError(Exception):
    """The mapping graph is not something we can convert."""


@dataclass(frozen=True, slots=True)
class Graph:
    """The instance-level wiring of one mapping."""

    mapping_name: str
    upstream: MappingABC[str, frozenset[str]]
    downstream: MappingABC[str, frozenset[str]]
    incoming: MappingABC[str, tuple[Connector, ...]]
    outgoing: MappingABC[str, tuple[Connector, ...]]

    def feeders(self, instance_name: str) -> frozenset[str]:
        return self.upstream.get(instance_name, frozenset())

    def consumers(self, instance_name: str) -> frozenset[str]:
        return self.downstream.get(instance_name, frozenset())

    def ancestors(self, instance_name: str) -> frozenset[str]:
        """Every instance that feeds this one, directly or indirectly."""
        seen: set[str] = set()
        queue = [instance_name]
        while queue:
            current = queue.pop()
            for feeder in self.feeders(current):
                if feeder not in seen:
                    seen.add(feeder)
                    queue.append(feeder)
        return frozenset(seen)

    def topological_order(self, instance_names: Iterable[str]) -> tuple[str, ...]:
        """Order a set of instances so every producer precedes its consumers.

        Ties are broken by name so that generated SQL is identical run to run.
        """
        subset = set(instance_names)
        remaining = {
            name: {f for f in self.feeders(name) if f in subset} for name in subset
        }

        ordered: list[str] = []
        while remaining:
            ready = sorted(
                name for name, feeders in remaining.items() if not feeders
            )
            if not ready:
                raise GraphError(
                    f"{self.mapping_name}: cycle between "
                    f"{', '.join(sorted(remaining))}"
                )
            for name in ready:
                ordered.append(name)
                del remaining[name]
            for feeders in remaining.values():
                feeders.difference_update(ready)

        return tuple(ordered)


def build_graph(mapping: Mapping) -> Graph:
    """Collapse the connectors of a mapping into node-level edges.

    PowerCenter writes one connector per column pair, so the 165 connectors of
    the staging mapping describe far fewer actual edges.
    """
    upstream: dict[str, set[str]] = defaultdict(set)
    downstream: dict[str, set[str]] = defaultdict(set)
    incoming: dict[str, list[Connector]] = defaultdict(list)
    outgoing: dict[str, list[Connector]] = defaultdict(list)

    for connector in mapping.connectors:
        source, target = connector.from_instance, connector.to_instance
        for end in (source, target):
            if end not in mapping.instances:
                raise GraphError(
                    f"{mapping.name}: connector references unknown instance {end!r}"
                )
        upstream[target].add(source)
        downstream[source].add(target)
        incoming[target].append(connector)
        outgoing[source].append(connector)

    return Graph(
        mapping_name=mapping.name,
        upstream=MappingProxyType({k: frozenset(v) for k, v in upstream.items()}),
        downstream=MappingProxyType({k: frozenset(v) for k, v in downstream.items()}),
        incoming=MappingProxyType({k: tuple(v) for k, v in incoming.items()}),
        outgoing=MappingProxyType({k: tuple(v) for k, v in outgoing.items()}),
    )


@dataclass(frozen=True, slots=True)
class Read:
    """A table a model reads, and how dbt should refer to it."""

    instance_name: str
    table: Table
    #: True when another model in this export writes this table, so the
    #: reference is ``ref()``; otherwise it is raw input and uses ``source()``.
    is_model: bool

    @property
    def reference(self) -> str:
        if self.is_model:
            return f"{{{{ ref('{model_name(self.table.name)}') }}}}"
        return (
            f"{{{{ source('{source_name(self.table.schema)}', "
            f"'{self.table.name}') }}}}"
        )


@dataclass(frozen=True, slots=True)
class ModelPlan:
    """One dbt model: a target plus everything feeding it."""

    name: str
    layer: str
    schema: str
    mapping_name: str
    target: Instance
    table: Table
    #: Every upstream instance, producers before consumers.
    steps: tuple[Instance, ...]
    reads: tuple[Read, ...]
    #: Steps this model shares with another model in the same mapping.
    shared_steps: tuple[str, ...]

    @property
    def depends_on(self) -> tuple[str, ...]:
        return tuple(
            sorted({model_name(r.table.name) for r in self.reads if r.is_model})
        )

    @property
    def path(self) -> str:
        return f"{self.layer}/{self.name}.sql"


def _model_tables(repo: Repository) -> frozenset[str]:
    """Names of tables written by some target, i.e. tables that become models."""
    return frozenset(
        instance.definition_name
        for mapping in repo.mappings
        for instance in mapping.target_instances
    )


def _reads_for(
    mapping: Mapping,
    repo: Repository,
    step_names: Iterable[str],
    model_tables: frozenset[str],
) -> tuple[Read, ...]:
    reads = []
    for name in sorted(step_names):
        instance = mapping.instances[name]
        if not instance.is_source:
            continue
        table = repo.sources.get(instance.definition_name)
        if table is None:
            raise GraphError(
                f"{mapping.name}: instance {name!r} reads unknown source "
                f"{instance.definition_name!r}"
            )
        reads.append(
            Read(
                instance_name=name,
                table=table,
                is_model=table.name in model_tables,
            )
        )
    return tuple(reads)


def plan_models(repo: Repository) -> tuple[ModelPlan, ...]:
    """Work out every dbt model this export produces, one per target."""
    model_tables = _model_tables(repo)
    plans: list[ModelPlan] = []

    for mapping in repo.mappings:
        graph = build_graph(mapping)

        # Which instances feed more than one target, and so would be duplicated
        # if every model inlined its whole upstream chain.
        feeds: dict[str, set[str]] = defaultdict(set)
        ancestry = {t.name: graph.ancestors(t.name) for t in mapping.target_instances}
        for target_name, ancestors in ancestry.items():
            for ancestor in ancestors:
                feeds[ancestor].add(target_name)

        for target in mapping.target_instances:
            table = repo.targets.get(target.definition_name)
            if table is None:
                raise GraphError(
                    f"{mapping.name}: target instance {target.name!r} writes "
                    f"unknown table {target.definition_name!r}"
                )

            step_names = ancestry[target.name]
            if not step_names:
                raise GraphError(
                    f"{mapping.name}: target {target.name!r} has nothing feeding it"
                )

            schema = schema_from_pre_sql(target.attributes.get("Pre SQL", "")) or ""
            plans.append(
                ModelPlan(
                    name=model_name(table.name),
                    layer=layer_for_schema(schema),
                    schema=schema,
                    mapping_name=mapping.name,
                    target=target,
                    table=table,
                    steps=tuple(
                        mapping.instances[n]
                        for n in graph.topological_order(step_names)
                    ),
                    reads=_reads_for(mapping, repo, step_names, model_tables),
                    shared_steps=tuple(
                        sorted(n for n in step_names if len(feeds[n]) > 1)
                    ),
                )
            )

    return tuple(plans)


def split_points(mapping: Mapping) -> dict[str, tuple[str, ...]]:
    """Steps where the pipeline forks towards different targets.

    A step forks only if its consumers head towards *different* targets.
    Feeding several consumers is not enough: a Router whose branches all
    reconverge further down has several consumers that reach exactly the same
    targets, so nothing is actually duplicated by leaving it inline.

    These are the natural places to cut a shared chain into its own
    intermediate model. Everything upstream of a fork is shared as well, but
    cutting at the fork is enough to stop it being duplicated.
    """
    graph = build_graph(mapping)
    targets = {t.name for t in mapping.target_instances}

    # Targets each other node can reach. A target reaches itself.
    reach: dict[str, set[str]] = {name: set() for name in mapping.instances}
    for target in targets:
        reach[target].add(target)
        for ancestor in graph.ancestors(target):
            reach[ancestor].add(target)

    forks = {}
    for name in mapping.instances:
        consumers = graph.consumers(name)
        if len(consumers) < 2:
            continue
        downstream_reach = {frozenset(reach[c]) for c in consumers}
        if len(downstream_reach) > 1:
            forks[name] = tuple(sorted(reach[name]))
    return forks


def unsupported_steps(plan: ModelPlan) -> tuple[str, ...]:
    """Steps whose transformation type we cannot convert."""
    return tuple(
        step.name
        for step in plan.steps
        if step.kind is Kind.UNKNOWN
    )
