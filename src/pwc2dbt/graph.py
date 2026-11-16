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
    """One dbt model: a leaf step plus everything feeding it.

    The leaf is usually a target. It can also be a *fork* — a step feeding
    several targets — which is promoted to its own intermediate model so the
    chain above it is built once instead of being copied into every consumer.
    """

    name: str
    layer: str
    schema: str
    mapping_name: str
    #: The target instance, or the fork step for an intermediate model.
    leaf: Instance
    #: The target table. None for an intermediate model, which has no table.
    table: Table | None
    #: Every upstream instance, producers before consumers.
    steps: tuple[Instance, ...]
    reads: tuple[Read, ...]
    #: Steps read as another model instead of being inlined:
    #: instance name -> that model's name.
    boundaries: MappingABC[str, str]
    #: The column names this model outputs.
    exposed: tuple[str, ...]
    #: Steps this model shares with another model in the same mapping.
    shared_steps: tuple[str, ...] = ()

    @property
    def is_intermediate(self) -> bool:
        return self.table is None

    @property
    def depends_on(self) -> tuple[str, ...]:
        refs = {model_name(r.table.name) for r in self.reads if r.is_model}
        refs |= set(self.boundaries.values())
        return tuple(sorted(refs))

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


def intermediate_name(instance_name: str) -> str:
    """Model name for a fork promoted to its own model.

    Named after the PowerCenter step so the generated model stays traceable
    back to the mapping. A semantic name cannot be inferred from the export.
    """
    return f"int_{instance_name.lower()}"


def _walk_back(
    graph: Graph,
    leaf: str,
    cut_at: frozenset[str],
    include_leaf: bool = False,
) -> tuple[frozenset[str], frozenset[str]]:
    """Collect the steps feeding ``leaf``, stopping at any step in ``cut_at``.

    Returns the steps to inline and the cut points reached. A cut point is
    included in the steps (it has to be read from somewhere) but its own
    feeders are not, because they belong to the cut point's model.

    ``include_leaf`` adds the leaf itself. An intermediate model needs it,
    because the fork step's own logic belongs to that model. A target does
    not: a target has no logic, only the column list of the final select.
    """
    steps: set[str] = set()
    reached: set[str] = set()
    queue = list(graph.feeders(leaf))
    seen = set(queue)
    if include_leaf:
        steps.add(leaf)

    while queue:
        name = queue.pop()
        steps.add(name)
        if name in cut_at:
            reached.add(name)
            continue
        for feeder in graph.feeders(name):
            if feeder not in seen:
                seen.add(feeder)
                queue.append(feeder)

    return frozenset(steps), frozenset(reached)


def _output_ports(mapping: Mapping, instance: Instance) -> tuple[str, ...]:
    """The column names a step exposes to whatever reads it."""
    transformation = mapping.transformation(instance.definition_name)
    if transformation is None:
        return ()
    names = [
        port.name.lower()
        for port in transformation.ports
        if port.is_output and not port.is_variable
    ]
    return tuple(dict.fromkeys(names))


@dataclass(frozen=True, slots=True)
class _Context:
    """Everything about one mapping that planning a model needs."""

    repo: Repository
    mapping: Mapping
    graph: Graph
    #: Steps promoted to their own model, so other models read them instead.
    forks: frozenset[str]
    #: Step name -> the targets it feeds, for reporting shared work.
    feeds: MappingABC[str, frozenset[str]]
    model_tables: frozenset[str]


def _context(repo: Repository, mapping: Mapping, extract_forks: bool) -> _Context:
    graph = build_graph(mapping)
    feeds: dict[str, set[str]] = defaultdict(set)
    for target in mapping.target_instances:
        for ancestor in graph.ancestors(target.name):
            feeds[ancestor].add(target.name)
    return _Context(
        repo=repo,
        mapping=mapping,
        graph=graph,
        forks=frozenset(split_points(mapping)) if extract_forks else frozenset(),
        feeds=MappingProxyType({k: frozenset(v) for k, v in feeds.items()}),
        model_tables=_model_tables(repo),
    )


def _plan(
    context: _Context,
    leaf: Instance,
    *,
    name: str,
    layer: str,
    schema: str,
    table: Table | None,
    exposed: tuple[str, ...],
) -> ModelPlan:
    """Plan one model: a leaf, and everything feeding it up to the next fork."""
    # An intermediate model owns the fork step itself; a target has no logic of
    # its own, only the column list of the final select. And a fork is never a
    # boundary for its own model.
    step_names, cuts = _walk_back(
        context.graph,
        leaf.name,
        context.forks - {leaf.name},
        include_leaf=table is None,
    )
    if not step_names:
        raise GraphError(
            f"{context.mapping.name}: {leaf.name!r} has nothing feeding it"
        )

    return ModelPlan(
        name=name,
        layer=layer,
        schema=schema,
        mapping_name=context.mapping.name,
        leaf=leaf,
        table=table,
        steps=tuple(
            context.mapping.instances[n]
            for n in context.graph.topological_order(step_names)
        ),
        reads=_reads_for(
            context.mapping, context.repo, step_names - cuts, context.model_tables
        ),
        boundaries=MappingProxyType(
            {n: intermediate_name(n) for n in sorted(cuts)}
        ),
        exposed=exposed,
        shared_steps=tuple(
            sorted(n for n in step_names if len(context.feeds.get(n, ())) > 1)
        ),
    )


def plan_models(repo: Repository, *, extract_forks: bool = True) -> tuple[ModelPlan, ...]:
    """Work out every dbt model this export produces.

    One model per target, plus one per fork when ``extract_forks`` is set. In
    the enriched mapping the five targets share 33 upstream steps; without the
    extra models each of those steps would be copied into up to five files.
    """
    plans: list[ModelPlan] = []

    for mapping in repo.mappings:
        context = _context(repo, mapping, extract_forks)

        # Intermediate models first, so they exist before anything refs them.
        for fork_name in sorted(context.forks):
            fork = mapping.instances[fork_name]
            plans.append(
                _plan(
                    context,
                    fork,
                    name=intermediate_name(fork_name),
                    layer="intermediate",
                    schema="",
                    table=None,
                    exposed=_output_ports(mapping, fork),
                )
            )

        for target in mapping.target_instances:
            table = repo.targets.get(target.definition_name)
            if table is None:
                raise GraphError(
                    f"{mapping.name}: target instance {target.name!r} writes "
                    f"unknown table {target.definition_name!r}"
                )
            schema = schema_from_pre_sql(target.attributes.get("Pre SQL", "")) or ""
            plans.append(
                _plan(
                    context,
                    target,
                    name=model_name(table.name),
                    layer=layer_for_schema(schema),
                    schema=schema,
                    table=table,
                    exposed=tuple(c.name.lower() for c in table.columns),
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
