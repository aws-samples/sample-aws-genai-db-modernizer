# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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
    def read_bytes(self, path: str) -> bytes:
        """Read a binary artifact and return its raw bytes."""
        ...

    def read_prefix(self, path: str, size: int) -> bytes:
        """Read at most the first *size* bytes of an artifact (#2345).

        For a reader that wants one field near the top of a large file. A store that can
        read a range overrides this; the default reads the whole artifact.
        """
        return self.read_bytes(path)[:size]

    @abstractmethod
    def write_bytes(self, path: str, data: bytes) -> None:
        """Write raw bytes as a binary artifact."""
        ...

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Return True if the artifact at *path* exists."""
        ...

    @abstractmethod
    def list_prefix(self, prefix: str) -> list[str]:
        """Return all artifact keys under *prefix*."""
        ...

    def delete(self, path: str) -> bool:
        """Delete the artifact at *path* where the store supports it; returns whether it
        did. Optional: a store that cannot delete (S3 without delete rights) keeps the
        default, which deletes nothing, and a caller must not rely on the file being gone
        (#1650)."""
        return False
