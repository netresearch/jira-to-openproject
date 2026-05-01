#!/usr/bin/env python3
"""Forward-only migration: normalise the persisted ``work_package`` mapping.

ADR-002 phase 3c unifies the polymorphic ``work_package`` mapping shape
into :class:`~src.models.mapping.WorkPackageMappingEntry`. This script
rewrites the existing on-disk
``var/data/work_package_mapping.json`` so every entry conforms to the
typed dict shape, dropping unrecoverable rows along the way.

The rewrite is *atomic* — we write to a tempfile in the same directory
and ``os.replace`` it into place. The destination's mode is mirrored
on the tempfile before the rename so file permissions survive the
swap.

Usage::

    uv run scripts/normalize_wp_mapping.py [--data-dir <path>] [--dry-run]

Behaviour:

* The script exits ``0`` on success or on benign no-ops (missing or
  malformed mapping) so it never breaks a user's pipeline.
* In ``--dry-run`` mode the file on disk is left untouched and a
  per-key change summary is printed to stdout.
* Without ``--dry-run`` the file is rewritten and the run is
  summarised with counts of dict-shaped, int-shaped, and dropped
  rows.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

# Allow ``uv run scripts/...`` and ``python scripts/...`` invocations
# without requiring the caller to set ``PYTHONPATH``: the project
# checkout root is the parent of ``scripts/``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.wp_mapping_normalizer import normalize_wp_mapping  # noqa: E402

DEFAULT_DATA_DIR = Path("var/data")
MAPPING_FILENAME = "work_package_mapping.json"

logger = logging.getLogger("normalize_wp_mapping")


def _classify_legacy_shapes(raw: dict[str, object]) -> tuple[int, int]:
    """Return ``(dict_count, int_count)`` for an already-loaded mapping.

    The classification is best-effort — the actual coercion happens in
    :func:`normalize_wp_mapping`, which also emits the dropped-count.
    Booleans are deliberately excluded from ``int_count`` because
    :meth:`WorkPackageMappingEntry.from_legacy` rejects them.
    """
    dict_count = 0
    int_count = 0
    for value in raw.values():
        if isinstance(value, dict):
            dict_count += 1
        elif isinstance(value, int) and not isinstance(value, bool):
            int_count += 1
    return dict_count, int_count


def _atomic_write_json(target: Path, payload: dict[str, dict[str, object]]) -> None:
    """Write ``payload`` to ``target`` atomically, preserving file mode.

    The tempfile is created in the same directory as ``target`` so the
    final ``os.replace`` is guaranteed to be atomic on every POSIX
    filesystem. We capture the source mode (if any) before writing so
    the rename does not surprise users running with a restrictive umask.
    """
    target_dir = target.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    source_mode: int | None = None
    try:
        source_mode = target.stat().st_mode & 0o777
    except FileNotFoundError:
        # First-run: no existing file means no mode to preserve.
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
            os.chmod(tmp_path, source_mode)
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup of the orphaned tempfile if anything
        # goes wrong before the atomic rename.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.warning("Failed to remove tempfile %s", tmp_path)
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Normalise var/data/work_package_mapping.json into the typed "
            "WorkPackageMappingEntry shape (ADR-002 phase 3c)."
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Directory holding work_package_mapping.json (default: var/data).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing the file.",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    """Entry point used by :func:`main` and by tests.

    Returns the process exit code. The function never raises on
    expected failure modes (missing file, malformed JSON, unwritable
    target) — those map to logged messages and a ``0`` exit so the
    script is safe to chain in user pipelines.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    data_dir: Path = args.data_dir
    target = data_dir / MAPPING_FILENAME

    if not target.exists():
        logger.info("No mapping file at %s; nothing to normalise.", target)
        return 0

    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s: %s", target, exc)
        return 0

    if not text.strip():
        logger.info("Mapping file %s is empty; nothing to normalise.", target)
        return 0

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Mapping file %s is malformed JSON: %s", target, exc)
        return 0

    if not isinstance(raw, dict):
        logger.warning(
            "Mapping file %s has unexpected top-level shape %s; expected dict.",
            target,
            type(raw).__name__,
        )
        return 0

    dict_count, int_count = _classify_legacy_shapes(raw)
    normalized, dropped = normalize_wp_mapping(raw)

    if args.dry_run:
        logger.info(
            "Dry-run: %d input entries → %d normalised (%d dict-shape, %d int-shape, %d dropped) in %s.",
            len(raw),
            len(normalized),
            dict_count,
            int_count,
            dropped,
            target,
        )
        # Surface a per-key diff for keys whose serialised form would
        # change. We compare against ``raw`` rather than ``normalized``
        # to keep the output focused on actual changes.
        for key, new_value in sorted(normalized.items()):
            old_value = raw.get(key)
            if old_value != new_value:
                print(f"would update {key}: {old_value!r} -> {new_value!r}")
        return 0

    try:
        _atomic_write_json(target, normalized)
    except OSError as exc:
        logger.warning("Could not write normalised mapping to %s: %s", target, exc)
        return 0

    logger.info(
        "Normalized %d input entries → %d kept (%d were already dict-shape, %d were int-shape, %d were dropped)",
        len(raw),
        len(normalized),
        dict_count,
        int_count,
        dropped,
    )
    return 0


def main() -> None:
    """Console-script entry point."""
    sys.exit(run())


if __name__ == "__main__":
    main()
