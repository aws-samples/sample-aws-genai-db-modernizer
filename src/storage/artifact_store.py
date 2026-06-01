"""Abstract base class for the ArtifactStore storage interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ArtifactStore(ABC):
    """Storage-agnostic artifact read/write interface."""

    @abstractmethod
    def read_json(self, path: str) -> dict:
        """Read a JSON artifact and return it as a dict."""
        ...

    @abstractmethod
    def write_json(self, path: str, data: dict) -> None:
        """Write a dict as a JSON artifact."""
        ...

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Return True if the artifact at *path* exists."""
        ...

    @abstractmethod
    def list_prefix(self, prefix: str) -> list[str]:
        """Return all artifact keys under *prefix*."""
        ...
