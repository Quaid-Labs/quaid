"""Core-owned in-memory bridge between datastore capabilities.

The bridge is a callback registry, not a persistence layer. Datastores register
the ids/capabilities they provide and the operations core may dispatch. Durable
links remain owned by the datastores themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Dict, Iterable, Mapping, Set


Callback = Callable[..., Any]


def _name(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _string_set(values: Iterable[Any] | None) -> Set[str]:
    out: Set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if text:
            out.add(text)
    return out


@dataclass(frozen=True)
class DatastoreBridgeRegistration:
    name: str
    provides: Set[str]
    consumes: Set[str]
    callbacks: Mapping[str, Callback]


class DatastoreBridge:
    """In-memory callback bridge for cross-datastore data requests."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._stores: Dict[str, DatastoreBridgeRegistration] = {}

    def register_store(
        self,
        *,
        name: str,
        provides: Iterable[Any] | None = None,
        consumes: Iterable[Any] | None = None,
        callbacks: Mapping[str, Callback] | None = None,
        replace: bool = True,
    ) -> DatastoreBridgeRegistration:
        store_name = _name(name, label="store name")
        callback_map = dict(callbacks or {})
        for op_name, callback in callback_map.items():
            _name(op_name, label="callback name")
            if not callable(callback):
                raise TypeError(f"Datastore bridge callback {store_name}.{op_name} is not callable")
        registration = DatastoreBridgeRegistration(
            name=store_name,
            provides=_string_set(provides),
            consumes=_string_set(consumes),
            callbacks=callback_map,
        )
        with self._lock:
            if not replace and store_name in self._stores:
                raise RuntimeError(f"Datastore bridge store already registered: {store_name}")
            self._stores[store_name] = registration
        return registration

    def call(self, store: str, operation: str, **kwargs: Any) -> Any:
        store_name = _name(store, label="store")
        op_name = _name(operation, label="operation")
        with self._lock:
            registration = self._stores.get(store_name)
            if registration is None:
                raise RuntimeError(f"Datastore bridge store is not registered: {store_name}")
            callback = registration.callbacks.get(op_name)
            if callback is None:
                raise RuntimeError(f"Datastore bridge route is not registered: {store_name}.{op_name}")
        return callback(**kwargs)

    def assert_route(self, store: str, operation: str) -> None:
        store_name = _name(store, label="store")
        op_name = _name(operation, label="operation")
        with self._lock:
            registration = self._stores.get(store_name)
            if registration is None:
                raise RuntimeError(f"Datastore bridge store is not registered: {store_name}")
            if op_name not in registration.callbacks:
                raise RuntimeError(f"Datastore bridge route is not registered: {store_name}.{op_name}")

    def list_routes(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                name: {
                    "provides": sorted(reg.provides),
                    "consumes": sorted(reg.consumes),
                    "callbacks": sorted(reg.callbacks),
                }
                for name, reg in sorted(self._stores.items())
            }

    def clear(self) -> None:
        with self._lock:
            self._stores.clear()


_DATASTORE_BRIDGE = DatastoreBridge()


def get_datastore_bridge() -> DatastoreBridge:
    return _DATASTORE_BRIDGE
