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

import copy
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_module_logger = logging.getLogger(__name__)


def _json_default(value: Any) -> Any:
    """Best-effort encoder for non-JSON-native objects.

    Mirrors :func:`src.utils.data_handler._json_default` so the repository
    handles the same shapes the legacy ``Mappings.set_mapping`` did.
    Falling back to ``str(value)`` keeps test fixtures (which sometimes
    pass ``unittest.mock.MagicMock`` instances through migration
    bookkeeping) writable rather than raising ``TypeError``.
    """
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseModel):
        return value.model_dump()
    return str(value)


class JsonFileMappingRepository:
    """Filesystem adapter implementing :class:`MappingRepository`.

    The repository keeps a small in-memory cache: once a mapping has
    been read or written through this instance, subsequent :meth:`get`
    calls return the cached payload without touching disk. Each
    :meth:`set` updates only the cache entry for the written name —
    other names retain their cached values. Missing names are NOT
    cached (so a transient :meth:`get` of an absent mapping does not
    pollute :meth:`all_names`).

    :meth:`get` and :meth:`set` deep-copy their payloads at the
    boundary so mutations on a returned dict cannot leak into the
    cache, and mutations of a dict passed into :meth:`set` after the
    call cannot leak in either. The cost is acceptable for migration
    mappings (sizes typically under 100k entries; deepcopy is
    micro-second per call at that scale) and the alternative —
    callers that mutate the live cache and silently corrupt later
    reads — is the kind of subtle bug a Repository pattern exists to
    prevent.

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
        ``name`` return a fresh deep copy of the cached payload without
        re-reading the file. Missing names are NOT cached (so they
        don't pollute :meth:`all_names`); a future :meth:`set` for the
        same name still works as expected. Malformed JSON is logged at
        warning level and yields an empty dict without caching.
        """
        if name in self._cache:
            # Deep copy on read: callers can mutate the returned dict
            # freely without poisoning the cache for the next call.
            return copy.deepcopy(self._cache[name])
        payload = self._read_from_disk(name)
        if payload:
            self._cache[name] = payload
        return copy.deepcopy(payload)

    def set(self, name: str, data: Mapping[str, Any]) -> None:
        """Persist ``data`` under ``name`` atomically.

        The write goes to a tempfile in the same directory and is
        promoted with :func:`os.replace`, so concurrent readers see
        either the previous or the new value, never a half-written
        file. The destination's file mode is mirrored on the tempfile
        before the rename so existing permissions survive.
        """
        # Deep copy: migration mappings are nested dicts (e.g.
        # ``{"PROJ-1": {"openproject_id": 7, ...}}``). A shallow
        # ``dict(data)`` would let a caller mutate a nested value
        # after :meth:`set` and silently corrupt the cached payload.
        payload: dict[str, Any] = copy.deepcopy(dict(data))
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
            # Log at DEBUG, not WARNING. The data directory is shared
            # with raw API cache files (jira_custom_fields.json,
            # jira_groups.json, …) that are legitimately list-shaped.
            # When Mappings.get_all_mappings() enumerates all_names() it
            # calls get() on every stem, including those raw-cache stems.
            # A WARNING here produces a flood of 16 identical lines on
            # every startup; DEBUG is sufficient for diagnostics because
            # the caller already handles the returned empty-dict safely.
            self._logger.debug(
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
            try:
                fh = os.fdopen(fd, "w", encoding="utf-8")
            except Exception:
                # ``os.fdopen`` failed before we could hand the fd to
                # the file object's lifecycle — close it explicitly so
                # the underlying descriptor isn't leaked.
                try:
                    os.close(fd)
                except OSError:
                    self._logger.warning(
                        "Failed to close tempfile fd for %s",
                        tmp_path,
                    )
                raise
            with fh:
                json.dump(payload, fh, indent=2, sort_keys=True, default=_json_default)
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
