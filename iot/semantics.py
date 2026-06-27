"""Load data-driven IoT resource API semantics.

Each JSON file under ``api_specs/`` describes one platform (POSIX, lwIP,
FreeRTOS, ...). A platform lists ``resources`` (acquire/release pairs such as
``socket``/``close`` or ``pbuf_alloc``/``pbuf_free``) and ``locks`` (take/give
pairs such as ``xSemaphoreTake``/``xSemaphoreGive``). Adding a new platform or
fixing an API is a JSON edit -- the CFG and data-flow engine never change.

Spec schema (per platform file)::

    {
      "platform": "lwip",
      "resources": [
        {
          "kind": "lwip_pbuf",
          "leak_type": "packet_buffer_not_freed",   # optional finding type
          "acquire": ["pbuf_alloc"],
          "acquire_result": "return",                 # "return" | "arg"
          "acquire_arg": 0,                            # used when result == "arg"
          "success": "non_null",                       # "non_null" | "non_negative"
          "release": ["pbuf_free"],
          "release_arg": 0
        }
      ],
      "locks": [
        {
          "kind": "freertos_mutex",
          "acquire": ["xSemaphoreTake"],
          "acquire_arg": 0,
          "release": ["xSemaphoreGive"],
          "release_arg": 0
        }
      ]
    }
"""

from dataclasses import dataclass, field
import json
from pathlib import Path


_DATA_DIR = Path(__file__).resolve().parent / "api_specs"

NON_NULL = "non_null"
NON_NEGATIVE = "non_negative"


@dataclass(frozen=True)
class ResourceSpec:
    """One acquire/release resource contract."""

    kind: str
    acquire_apis: frozenset[str]
    release_apis: frozenset[str]
    acquire_result: str = "return"  # "return" or "arg"
    acquire_arg: int = 0
    release_arg: int = 0
    success: str = NON_NULL
    leak_type: str | None = None
    platform: str = "unknown"
    # Project-inferred static wrappers are visible only in their source file.
    scope_file: str | None = None

    @property
    def leak_finding_type(self) -> str:
        return self.leak_type or f"{self.kind}_not_released_on_path"


@dataclass(frozen=True)
class LockSpec:
    """One lock acquire/release contract (take/give style)."""

    kind: str
    acquire_apis: frozenset[str]
    release_apis: frozenset[str]
    acquire_arg: int = 0
    release_arg: int = 0
    success: str = "zero"
    leak_type: str = "lock_not_released_on_path"
    platform: str = "unknown"


@dataclass(frozen=True)
class SinkSpec:
    """A project function that takes ownership of a pointer argument.

    Inferred structurally: parameter ``arg`` of function ``name`` escapes into
    a field/global/list inside the body (e.g. ``MQTTAsync_addCommand(conn)`` ->
    ``ListAppend(queue, conn, ...)``). Passing a tracked resource there hands
    ownership off, so the caller is no longer responsible for releasing it.
    """

    name: str
    arg: int = 0
    # Project-inferred static sinks are visible only in their source file.
    scope_file: str | None = None


@dataclass(frozen=True)
class Transition:
    """One legal protocol transition: ``api`` moves the object at ``arg`` from
    any of ``from_states`` to ``to_state``."""

    api: str
    arg: int
    from_states: frozenset[str]
    to_state: str


@dataclass(frozen=True)
class ProtocolSpec:
    """A typestate contract for an object kind (init -> ... -> destroy).

    ``create_apis`` put the object into ``initial_state``; each ``Transition``
    says which API is legal in which states. An API applied to an object whose
    state is not in its ``from_states`` is an ``invalid_protocol_transition``.
    """

    kind: str
    create_apis: frozenset[str]
    initial_state: str
    transitions: tuple[Transition, ...]
    create_result: str = "return"  # "return" or "arg"
    create_arg: int = 0
    platform: str = "unknown"


@dataclass(frozen=True)
class IoTSemantics:
    """Indexed view over all loaded platform specs."""

    resources: tuple[ResourceSpec, ...] = ()
    locks: tuple[LockSpec, ...] = ()
    protocols: tuple[ProtocolSpec, ...] = ()
    # name -> spec lookups, built once at load.
    _acquire_index: dict[str, ResourceSpec] = field(default_factory=dict)
    _release_index: dict[str, tuple[ResourceSpec, ...]] = field(default_factory=dict)
    _lock_acquire_index: dict[str, LockSpec] = field(default_factory=dict)
    _lock_release_index: dict[str, tuple[LockSpec, ...]] = field(default_factory=dict)
    _sink_index: dict[str, tuple[SinkSpec, ...]] = field(default_factory=dict)
    _protocol_create_index: dict[str, ProtocolSpec] = field(default_factory=dict)
    _protocol_transition_index: dict[str, tuple[tuple[ProtocolSpec, Transition], ...]] = (
        field(default_factory=dict)
    )

    def acquire_spec(self, api: str) -> ResourceSpec | None:
        return self._acquire_index.get(api)

    def release_specs(self, api: str) -> tuple[ResourceSpec, ...]:
        return self._release_index.get(api, ())

    def lock_acquire_spec(self, api: str) -> LockSpec | None:
        return self._lock_acquire_index.get(api)

    def lock_release_specs(self, api: str) -> tuple[LockSpec, ...]:
        return self._lock_release_index.get(api, ())

    def sink_specs(self, api: str) -> tuple[SinkSpec, ...]:
        return self._sink_index.get(api, ())

    def protocol_create_spec(self, api: str) -> ProtocolSpec | None:
        return self._protocol_create_index.get(api)

    def protocol_transitions(
        self, api: str
    ) -> tuple[tuple[ProtocolSpec, Transition], ...]:
        return self._protocol_transition_index.get(api, ())

    def platforms(self) -> list[str]:
        names = {spec.platform for spec in self.resources}
        names |= {spec.platform for spec in self.locks}
        names |= {spec.platform for spec in self.protocols}
        return sorted(names)

    def augmented(
        self,
        *,
        wrappers: "list[ResourceSpec]" = (),
        sinks: "list[SinkSpec]" = (),
        source_file: str | None = None,
    ) -> "IoTSemantics":
        """Return a copy whose indexes also recognize project-inferred release
        wrappers and ownership sinks.

        Both only extend lookups; the ``resources``/``locks`` tuples are
        unchanged, so leak-type overrides, success contracts, and
        ``_is_non_null_resource`` keep using the real specs. File-local
        (``static``) inferences whose ``scope_file`` differs from
        ``source_file`` are skipped.
        """
        clone = IoTSemantics(
            resources=self.resources, locks=self.locks, protocols=self.protocols
        )
        _build_indexes(clone)
        for spec in wrappers:
            if spec.scope_file is not None and spec.scope_file != source_file:
                continue
            for api in spec.release_apis:
                clone._release_index[api] = clone._release_index.get(api, ()) + (spec,)
        for sink in sinks:
            if sink.scope_file is not None and sink.scope_file != source_file:
                continue
            clone._sink_index[sink.name] = clone._sink_index.get(sink.name, ()) + (sink,)
        return clone

    def with_release_wrappers(
        self,
        wrapper_specs: "list[ResourceSpec]",
        *,
        source_file: str | None = None,
    ) -> "IoTSemantics":
        """Backward-compatible shorthand for ``augmented(wrappers=...)``."""
        return self.augmented(wrappers=wrapper_specs, source_file=source_file)


def _build_indexes(semantics: IoTSemantics) -> IoTSemantics:
    release_index: dict[str, list[ResourceSpec]] = {}
    lock_release_index: dict[str, list[LockSpec]] = {}
    for spec in semantics.resources:
        for api in spec.acquire_apis:
            semantics._acquire_index[api] = spec
        for api in spec.release_apis:
            release_index.setdefault(api, []).append(spec)
    for lock in semantics.locks:
        for api in lock.acquire_apis:
            semantics._lock_acquire_index[api] = lock
        for api in lock.release_apis:
            lock_release_index.setdefault(api, []).append(lock)
    semantics._release_index.update(
        {api: tuple(specs) for api, specs in release_index.items()}
    )
    semantics._lock_release_index.update(
        {api: tuple(specs) for api, specs in lock_release_index.items()}
    )
    transition_index: dict[str, list[tuple[ProtocolSpec, Transition]]] = {}
    for proto in semantics.protocols:
        for api in proto.create_apis:
            semantics._protocol_create_index[api] = proto
        for transition in proto.transitions:
            transition_index.setdefault(transition.api, []).append((proto, transition))
    semantics._protocol_transition_index.update(
        {api: tuple(pairs) for api, pairs in transition_index.items()}
    )
    return semantics


def _resource_from_dict(data: dict, platform: str) -> ResourceSpec:
    acquire_result = data.get("acquire_result", "return")
    return ResourceSpec(
        kind=data["kind"],
        acquire_apis=frozenset(data.get("acquire", [])),
        release_apis=frozenset(data.get("release", [])),
        acquire_result=acquire_result,
        acquire_arg=int(data.get("acquire_arg", 0)),
        release_arg=int(data.get("release_arg", 0)),
        # Return-value resources are usually pointer-like; out-parameter
        # initializers conventionally return zero on success.
        success=data.get(
            "success", "zero" if acquire_result == "arg" else NON_NULL
        ),
        leak_type=data.get("leak_type"),
        platform=platform,
    )


def _lock_from_dict(data: dict, platform: str) -> LockSpec:
    return LockSpec(
        kind=data["kind"],
        acquire_apis=frozenset(data.get("acquire", [])),
        release_apis=frozenset(data.get("release", [])),
        acquire_arg=int(data.get("acquire_arg", 0)),
        release_arg=int(data.get("release_arg", 0)),
        success=data.get("success", "zero"),
        leak_type=data.get("leak_type", "lock_not_released_on_path"),
        platform=platform,
    )


def _protocol_from_dict(data: dict, platform: str) -> ProtocolSpec:
    transitions = tuple(
        Transition(
            api=t["api"],
            arg=int(t.get("arg", 0)),
            from_states=frozenset(t.get("from", [])),
            to_state=t["to"],
        )
        for t in data.get("transitions", [])
    )
    return ProtocolSpec(
        kind=data["kind"],
        create_apis=frozenset(data.get("create", [])),
        initial_state=data["initial_state"],
        transitions=transitions,
        create_result=data.get("create_result", "return"),
        create_arg=int(data.get("create_arg", 0)),
        platform=platform,
    )


def load_iot_semantics(paths: list[str | Path] | str | Path | None = None) -> IoTSemantics:
    """Load and index IoT semantics from one or more JSON spec files.

    With no argument, every ``*.json`` under ``iot/api_specs/`` is loaded and
    merged, so all bundled platforms are active by default.
    """
    if paths is None:
        spec_files = sorted(_DATA_DIR.glob("*.json"))
    elif isinstance(paths, (str, Path)):
        spec_files = [Path(paths)]
    else:
        spec_files = [Path(p) for p in paths]

    resources: list[ResourceSpec] = []
    locks: list[LockSpec] = []
    protocols: list[ProtocolSpec] = []
    for spec_file in spec_files:
        data = json.loads(Path(spec_file).read_text(encoding="utf-8"))
        platform = data.get("platform", Path(spec_file).stem)
        for entry in data.get("resources", []):
            resources.append(_resource_from_dict(entry, platform))
        for entry in data.get("locks", []):
            locks.append(_lock_from_dict(entry, platform))
        for entry in data.get("protocols", []):
            protocols.append(_protocol_from_dict(entry, platform))

    semantics = IoTSemantics(
        resources=tuple(resources), locks=tuple(locks), protocols=tuple(protocols)
    )
    return _build_indexes(semantics)
