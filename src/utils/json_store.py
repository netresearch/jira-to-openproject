"""Filesystem-backed JSON store with logger-aware diagnostics.

Replaces the ``_load_from_json`` / ``_save_to_json`` helpers that used to live
on ``BaseMigration``. Files are resolved relative to a base directory passed at
construction time, so per-migration code does not need to know about
``var/data/`` paths.

Extracted from ``BaseMigration`` per ADR-002 phase 1.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_module_logger = logging.getLogger(__name__)


class JsonStore:
    """Tiny wrapper around ``json.load`` / ``json.dump`` with consistent diagnostics."""

    def __init__(self, base_dir: Path, logger: logging.Logger | None = None) -> None:
        self.base_dir = base_dir
        self._logger = logger or _module_logger

    def load(self, filename: Path | str, default: Any = None) -> Any:
        """Load JSON from ``base_dir / filename``.

        Returns ``default`` if the file does not exist, is empty, or fails to
        parse. All failure modes are logged.
        """
        filepath = self.base_dir / Path(filename)
        try:
            with filepath.open("r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            self._logger.debug("File does not exist: %s", filepath)
            return default
        except json.JSONDecodeError as e:
            if filepath.exists() and filepath.stat().st_size == 0:
                self._logger.debug("File is empty: %s", filepath)
            else:
                self._logger.exception("JSON decode error in %s: %s", filepath, e)
            return default
        except Exception as e:
            self._logger.exception("Unexpected error loading %s: %s", filepath, e)
            return default

    def save(self, data: Any, filename: Path | str) -> Path:
        """Write ``data`` as pretty-printed JSON to ``base_dir / filename``.

        Creates parent directories as needed. Returns the resolved path.
        """
        filepath = self.base_dir / Path(filename)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with filepath.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self._logger.debug("Saved data to %s", filepath)
        return filepath
