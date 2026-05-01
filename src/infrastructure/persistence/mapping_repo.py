"""JSON-file-backed :class:`MappingRepository` adapter (ADR-002 phase 4a).

Mappings are stored one-per-file under a configurable data directory
using the convention::

    <data_dir>/<name>.json

where ``name`` is the *full filename stem* passed to :meth:`get`,
:meth:`set`, and friends. Today's :class:`src.mappings.mappings.Mappings`
class hardcodes filenames like ``user_mapping.json`` and
``project_mapping.json``; this adapter expects callers to pass
``"user_mapping"`` / ``"project_mapping"`` as the name. PR 4b will
update consumers to use the full stem; the legacy
:meth:`Mappings.get_mapping` helper does the ``"user"`` →
``"user_mapping"`` conversion under the hood today.

Writes go through an atomic ``tempfile + os.replace`` dance that
mirrors the destination's file mode onto the new file before the
rename, so file permissions survive the swap. The same pattern is used
in :mod:`scripts.normalize_wp_mapping`; we keep the implementations
parallel rather than extracting a shared helper because the script is a
forward-only one-shot whose semantics we explicitly do not want to
couple to runtime adapter behaviour.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

_module_logger = logging.getLogger(__name__)


class JsonFileMappingRepository:
    """Filesystem adapter implementing :class:`MappingRepository`.

    The repository keeps a small in-memory cache: once a mapping has
    been read or written through this instance, subsequent :meth:`get`
    calls return the cached payload without touching disk. The cache is
    invalidated on every :meth:`set` (because we just wrote a fresh
    value) and on construction (cold start).

    The cache is **per-instance** — multiple processes or instances
    pointed at the same data directory will not see each other's
    in-memory writes until the next disk read. That matches the
    original :class:`Mappings` behaviour and is sufficient for the
    single-process migration runtime.

    The adapter satisfies :class:`src.domain.repositories.MappingRepository`
    structurally; we deliberately do not inherit from the Protocol so
    importers do not pay the runtime cost of Protocol metaclass
    resolution on every instantiation.
    """

    JSON_SUFFIX = ".json"

    def __init__(
        self,
        data_dir: Path,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create a repository pointed at ``data_dir``.

        Args:
            data_dir: Directory holding ``<name>.json`` files. Created on
                first :meth:`set` if it does not yet exist.
            logger: Optional logger for diagnostics. Defaults to a module
                logger so adapter messages are filterable independently
                of the rest of the codebase.

        """
        self._data_dir: Path = data_dir
        self._logger: logging.Logger = logger or _module_logger
        # Cache of name → payload. Populated lazily; invalidated on set.
        self._cache: dict[str, dict[str, Any]] = {}

    # ── Public Protocol surface ──────────────────────────────────────

    def get(self, name: str) -> dict[str, Any]:
        """Return the named mapping, or an empty dict if missing.

        Caches the result in-memory; subsequent calls for the same
        ``name`` return the cached payload without re-reading the file.
        Missing files and malformed JSON both yield an empty dict; the
        latter is logged at warning level so operators notice corrupt
        state without crashing the migration.
        """
        if name in self._cache:
            return self._cache[name]
        payload = self._read_from_disk(name)
        self._cache[name] = payload
        return payload

    def set(self, name: str, data: Mapping[str, Any]) -> None:
        """Persist ``data`` under ``name`` atomically.

        The write goes to a tempfile in the same directory and is
        promoted with :func:`os.replace`, so concurrent readers see
        either the previous or the new value, never a half-written
        file. The destination's file mode is mirrored on the tempfile
        before the rename so existing permissions survive.
        """
        # Defensive copy: the in-memory cache must not alias the
        # caller's payload, otherwise their mutations would silently
        # affect future ``get`` results.
        payload: dict[str, Any] = dict(data)
        target = self._path_for(name)
        self._atomic_write_json(target, payload)
        self._cache[name] = payload

    def has(self, name: str) -> bool:
        """Whether a non-empty mapping is stored under ``name``.

        We re-use :meth:`get` so the in-memory cache stays consistent
        with subsequent reads; a slow first call (disk I/O) is the
        worst case.
        """
        return bool(self.get(name))

    def all_names(self) -> list[str]:
        """List every ``<name>.json`` stem in the data directory.

        Returns the union of in-memory cached names and on-disk files,
        sorted alphabetically for stable output. The data directory not
        existing yet is treated as "no mappings", not an error.
        """
        names: set[str] = set(self._cache)
        if self._data_dir.is_dir():
            for entry in self._data_dir.iterdir():
                if entry.is_file() and entry.suffix == self.JSON_SUFFIX:
                    names.add(entry.stem)
        return sorted(names)

    # ── Internal helpers ─────────────────────────────────────────────

    def _path_for(self, name: str) -> Path:
        """Resolve ``name`` to ``<data_dir>/<name>.json``."""
        return self._data_dir / f"{name}{self.JSON_SUFFIX}"

    def _read_from_disk(self, name: str) -> dict[str, Any]:
        """Read and parse ``<data_dir>/<name>.json``.

        Returns an empty dict for any non-fatal failure (missing file,
        malformed JSON, non-dict top-level shape). Malformed and
        wrong-shape payloads emit a warning so the operator can
        investigate; missing files are intentionally silent because
        cold-start migrations expect them.
        """
        path = self._path_for(name)
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            self._logger.warning(
                "Mapping file %s is malformed JSON: %s",
                path,
                exc,
            )
            return {}
        except OSError as exc:
            self._logger.warning("Could not read mapping file %s: %s", path, exc)
            return {}

        if not isinstance(raw, dict):
            self._logger.warning(
                "Mapping file %s has unexpected top-level shape %s; expected dict.",
                path,
                type(raw).__name__,
            )
            return {}
        return raw

    def _atomic_write_json(
        self,
        target: Path,
        payload: dict[str, Any],
    ) -> None:
        """Write ``payload`` to ``target`` atomically, preserving file mode.

        Mirrors the helper in :mod:`scripts.normalize_wp_mapping` so the
        runtime adapter and the one-shot migration script behave
        identically with respect to permissions and atomicity.
        """
        target_dir = target.parent
        target_dir.mkdir(parents=True, exist_ok=True)

        # Capture the existing destination mode (if any) so we can
        # restore it after the rename. First-run writes have nothing to
        # mirror.
        source_mode: int | None
        try:
            source_mode = target.stat().st_mode & 0o777
        except FileNotFoundError:
            source_mode = None

        fd, tmp_path_str = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target_dir),
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            if source_mode is not None:
                tmp_path.chmod(source_mode)
            tmp_path.replace(target)
        except Exception:
            # Best-effort cleanup of the orphaned tempfile if anything
            # goes wrong before the atomic rename.
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    self._logger.warning(
                        "Failed to remove tempfile %s",
                        tmp_path,
                    )
            raise


__all__ = ["JsonFileMappingRepository"]
