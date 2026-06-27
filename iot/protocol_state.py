"""Immutable typestate map for IoT protocol-order analysis.

Tracks, per variable, the current protocol state of an object (e.g. an MQTT
client moving ``initialized -> connected -> disconnected -> destroyed``). The
forward data-flow solver merges these at CFG joins; when two paths disagree on
a variable's state the merge yields :data:`UNKNOWN`, which suppresses
transition checking on that variable so conservative joins do not produce false
positives.
"""

from dataclasses import dataclass


# A variable whose state differs across merged paths. Operations on an UNKNOWN
# object are never flagged -- the analysis cannot prove an illegal order.
UNKNOWN = "?"


@dataclass(frozen=True)
class ProtocolState:
    """Immutable map from C variable names to protocol state strings."""

    states: tuple[tuple[str, str], ...] = ()

    def get(self, variable: str) -> str | None:
        for name, state in self.states:
            if name == variable:
                return state
        return None

    def set(self, variable: str, state: str) -> "ProtocolState":
        data = dict(self.states)
        data[variable] = state
        return ProtocolState(tuple(sorted(data.items())))

    def has(self, variable: str) -> bool:
        return any(name == variable for name, _ in self.states)


def merge_protocol_states(states: list[ProtocolState]) -> ProtocolState:
    """Merge typestate maps from CFG predecessors.

    A variable present with a single agreed state keeps it; a variable that is
    missing on some path or disagreed-upon becomes :data:`UNKNOWN`.
    """
    if not states:
        return ProtocolState()
    maps = [dict(state.states) for state in states]
    names = {name for m in maps for name in m}
    merged: dict[str, str] = {}
    for name in names:
        values = {m.get(name) for m in maps}
        if len(values) == 1 and None not in values:
            merged[name] = next(iter(values))
        else:
            merged[name] = UNKNOWN
    return ProtocolState(tuple(sorted(merged.items())))
