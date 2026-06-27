"""Immutable resource state for IoT lifetime analysis.

Models the common ``acquire -> active -> release`` shape shared by IoT
resources (heap blocks, sockets, packet buffers, locks, handles). State is an
immutable map from C variable names to per-resource states so the forward
data-flow solver can merge states at CFG join points without aliasing.
"""

from dataclasses import dataclass, field


ACTIVE = "active"
RELEASED = "released"
ESCAPED = "escaped"
DECLARED = "declared"
MIXED = "mixed"

DECLARED_KIND = "declared"


@dataclass(frozen=True)
class Resource:
    """Lifetime state for one tracked IoT resource."""

    kind: str
    state: str = ACTIVE
    line: int | None = None
    api: str | None = None
    transition_line: int | None = None
    transition_api: str | None = None
    status_variable: str | None = None
    success_condition: str | None = None
    alternatives: frozenset[str] = field(default_factory=frozenset)

    @property
    def maybe_active(self) -> bool:
        return self.state == ACTIVE or (
            self.state == MIXED and ACTIVE in self.alternatives
        )

    @property
    def maybe_released(self) -> bool:
        return self.state == RELEASED or (
            self.state == MIXED and RELEASED in self.alternatives
        )


@dataclass(frozen=True)
class ResourceState:
    """Immutable map from C variable names to IoT resource states."""

    resources: tuple[tuple[str, Resource], ...] = ()
    aliases: tuple[tuple[str, str], ...] = ()

    def resolve(self, variable: str) -> str:
        """Return the canonical variable for a simple alias chain."""
        aliases = dict(self.aliases)
        seen = set()
        current = variable
        while current in aliases and current not in seen:
            seen.add(current)
            current = aliases[current]
        return current

    def get(self, variable: str) -> Resource | None:
        return dict(self.resources).get(self.resolve(variable))

    def set(
        self,
        variable: str,
        kind: str,
        *,
        state: str = ACTIVE,
        line: int | None = None,
        api: str | None = None,
        status_variable: str | None = None,
        success_condition: str | None = None,
    ) -> "ResourceState":
        data = dict(self.resources)
        existing = data.get(variable)
        data[variable] = Resource(
            kind=kind,
            state=state,
            line=line,
            api=api or (existing.api if existing else None),
            status_variable=status_variable,
            success_condition=success_condition,
        )
        aliases = dict(self.aliases)
        aliases.pop(variable, None)
        return ResourceState(tuple(sorted(data.items())), tuple(sorted(aliases.items())))

    def release(
        self,
        variable: str,
        *,
        line: int | None = None,
        api: str | None = None,
    ) -> "ResourceState":
        data = dict(self.resources)
        canonical = self.resolve(variable)
        existing = data.get(canonical)
        # A bare ``declared`` placeholder is not an acquired resource (it only
        # marks a local for escape disambiguation), so it must never be turned
        # into RELEASED -- otherwise a failed-acquire/``if(p)`` refinement on a
        # borrowed pointer would fabricate a use-after-release.
        if existing is None or existing.kind == DECLARED_KIND:
            return self
        data[canonical] = Resource(
            kind=existing.kind,
            state=RELEASED,
            line=existing.line,
            api=existing.api,
            transition_line=line,
            transition_api=api,
            status_variable=existing.status_variable,
            success_condition=existing.success_condition,
        )
        return ResourceState(tuple(sorted(data.items())), self.aliases)

    def escape(
        self,
        variable: str,
        *,
        line: int | None = None,
        api: str | None = "escape",
    ) -> "ResourceState":
        """Mark a resource as escaped (stored into a field/global/out-param).

        An escaped resource has had its ownership handed to a longer-lived
        location, so releasing it is no longer this function's responsibility.
        """
        data = dict(self.resources)
        canonical = self.resolve(variable)
        existing = data.get(canonical)
        if existing is None:
            return self
        data[canonical] = Resource(
            kind=existing.kind,
            state=ESCAPED,
            line=existing.line,
            api=existing.api,
            transition_line=line,
            transition_api=api,
            status_variable=existing.status_variable,
            success_condition=existing.success_condition,
        )
        return ResourceState(tuple(sorted(data.items())), self.aliases)

    def note_declared(self, variable: str) -> "ResourceState":
        """Record that a local was declared, without a resource yet.

        Lets escape detection tell a re-assigned local (declared here, so a
        plain local) from a bare assignment to a file-scope/global name.
        """
        if any(name == variable for name, _ in self.resources):
            return self
        data = dict(self.resources)
        data[variable] = Resource(kind=DECLARED_KIND, state=DECLARED)
        return ResourceState(tuple(sorted(data.items())), self.aliases)

    def alias(self, variable: str, target: str) -> "ResourceState":
        """Return a state where ``variable`` aliases ``target``."""
        target = self.resolve(target)
        if variable == target:
            return self
        data = dict(self.resources)
        data.pop(variable, None)
        aliases = dict(self.aliases)
        aliases[variable] = target
        return ResourceState(tuple(sorted(data.items())), tuple(sorted(aliases.items())))

    def has(self, variable: str) -> bool:
        return any(name == variable for name, _ in self.resources) or any(
            name == variable for name, _ in self.aliases
        )

    def active_resources(self) -> dict[str, Resource]:
        return {
            name: resource
            for name, resource in self.resources
            if resource.maybe_active
        }


def merge_resource_states(states: list[ResourceState]) -> ResourceState:
    """Merge IoT resource states from multiple CFG predecessors."""
    if not states:
        return ResourceState()
    maps = [dict(state.resources) for state in states]
    names = sorted({name for resources in maps for name in resources})
    merged = {}
    for name in names:
        values = [resources[name] for resources in maps if name in resources]
        merged[name] = _merge_resource(values)
    aliases = _merge_aliases(states)
    return ResourceState(tuple(merged.items()), tuple(sorted(aliases.items())))


def _merge_resource(values: list[Resource]) -> Resource:
    first = values[0]
    if all(value == first for value in values):
        return first
    # A bare ``declared`` placeholder is a bottom value: it must not override a
    # real resource kind acquired on another path, or a conditional acquire
    # would be lost. Determine the kind from the real (non-declared) resources.
    real = [value for value in values if value.state != DECLARED]
    pool = real if real else values
    # Pick the acquire site deterministically (smallest line, then api) so the
    # merged value does not depend on predecessor visitation order.
    base = min(pool, key=lambda r: (r.line if r.line is not None else -1, r.api or ""))
    real_kinds = {value.kind for value in real}
    kind = base.kind if len(real_kinds) <= 1 else "resource"
    # Flatten: a MIXED input contributes its own concrete alternatives, never the
    # literal "mixed", so re-merging is idempotent and the set stays bounded.
    alternatives = frozenset(
        alt
        for value in values
        for alt in (value.alternatives if value.state == MIXED else {value.state})
    )
    # Carry only the stable acquire site (line/api). Per-transition provenance
    # (transition_line/api, saved-status fields) is ambiguous at a join and, if
    # carried, oscillates across loop back-edges so the data-flow never reaches a
    # fixed point; drop it here.
    return Resource(
        kind=kind,
        state=MIXED,
        line=base.line,
        api=base.api,
        alternatives=alternatives,
    )


def _merge_aliases(states: list[ResourceState]) -> dict[str, str]:
    alias_maps = [dict(state.aliases) for state in states]
    names = sorted({name for aliases in alias_maps for name in aliases})
    result = {}
    for name in names:
        targets = {aliases.get(name) for aliases in alias_maps}
        if len(targets) == 1:
            target = targets.pop()
            if target is not None:
                result[name] = target
    return result
