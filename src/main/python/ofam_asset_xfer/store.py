from __future__ import annotations

import json
import os
import posixpath
from dataclasses import dataclass
from typing import Any, Tuple

import fsspec  # type: ignore[import-untyped]


def _join_uri(base_uri: str, *parts: str) -> str:
    # Safe join for both local paths and fsspec URIs.
    if "://" in base_uri:
        path = base_uri
        for p in parts:
            path = posixpath.join(path.rstrip("/"), p.lstrip("/"))
        return path
    return os.path.join(base_uri, *parts)


@dataclass(frozen=True)
class ArtifactStore:
    base_uri: str

    def _fs_and_path(self, uri: str) -> Tuple[Any, str]:
        fs, path = fsspec.core.url_to_fs(uri)
        return fs, path

    def ensure_dir(self, rel_dir: str = "") -> None:
        """Ensure the directory exists (best-effort for object stores)."""
        uri = _join_uri(self.base_uri, rel_dir) if rel_dir else self.base_uri
        fs, path = self._fs_and_path(uri)
        # Many object stores don't have real directories; makedirs is best-effort.
        try:
            fs.makedirs(path, exist_ok=True)
        except Exception:
            # Ignore if backend doesn't support directories.
            pass

    def write_text(self, rel_path: str, text: str, encoding: str = "utf-8") -> str:
        """Write text content to a file at the given relative path."""
        uri = _join_uri(self.base_uri, rel_path)
        fs, path = self._fs_and_path(uri)
        parent = os.path.dirname(path)
        try:
            fs.makedirs(parent, exist_ok=True)
        except Exception:
            pass
        with fs.open(path, "w", encoding=encoding) as f:
            f.write(text)
        return uri

    def write_json(self, rel_path: str, obj: Any, indent: int = 2) -> str:
        """Serialise obj as JSON and write to the given relative path."""
        return self.write_text(rel_path, json.dumps(obj, indent=indent, sort_keys=True))

    def read_text(self, uri_or_rel: str, encoding: str = "utf-8") -> str:
        """Read and return text from a URI or relative path."""
        uri = (
            uri_or_rel
            if "://" in uri_or_rel or os.path.isabs(uri_or_rel)
            else _join_uri(self.base_uri, uri_or_rel)
        )
        fs, path = self._fs_and_path(uri)
        with fs.open(path, "r", encoding=encoding) as f:
            return str(f.read())

    def read_json(self, uri_or_rel: str) -> Any:
        """Read and parse JSON from a URI or relative path."""
        return json.loads(self.read_text(uri_or_rel))
