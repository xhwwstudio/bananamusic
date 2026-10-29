from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class StorageRef:
    provider_id: str
    path: str
    root: str = 'default'
    object_id: str | None = None


@dataclass(slots=True)
class StorageEntry:
    provider_id: str
    path: str
    name: str
    is_dir: bool
    size: int | None = None
    modified_at: str | None = None
    object_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StorageProviderConfig:
    id: str
    type: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
