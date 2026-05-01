"""Discriminated-union step results for the j2o domain layer.

Phase 8a of ADR-002 ("Polish") introduces a typed, three-arm result union
to replace the ad-hoc :class:`src.models.component_results.ComponentResult`
dictionary-of-everything envelope. The intent is to give downstream code
three crisp arms to pattern-match on instead of inspecting ~30 mostly-unused
counters on every legacy result.

This module is *pure addition*. No migration entry-point or call site is
modified in this PR; the legacy ``ComponentResult`` class is untouched.
The bridge between the two worlds is the :func:`from_component_result`
and :func:`to_component_result` adapter pair, documented below.

Pattern matrix (per ADR-002):

* :class:`Success` — a step that completed and produced a usable result.
  Carries optional ``data``, a human ``message``, free-form ``counts``
  for telemetry, and any non-fatal ``warnings``.
* :class:`Skipped` — a step that intentionally did no work (e.g. nothing
  to migrate, feature disabled). Carries a structured ``reason`` and an
  optional human ``message``.
* :class:`Failed` — a step that raised or could not complete. Carries a
  primary ``error`` string, the full ``errors`` list (for batched
  operations that collect multiple failures), and any partial ``counts``.

The union is discriminated by the literal ``kind`` field, so Pydantic v2
will pick the right concrete model when validating dict input::

    StepResult.model_validate({"kind": "skipped", "reason": "no work"})

Future PRs (phase 8b+) can incrementally migrate call sites; until then
both shapes coexist and round-trip via the adapter helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from src.models.component_results import ComponentResult


class Success(BaseModel):
    """A successful migration step.

    Attributes:
        kind: Discriminator literal — always ``"success"``.
        data: Optional payload returned by the step (e.g. a list of
            created records, a mapping dict, or a summary object). Free
            shape — callers who care about the type should narrow it.
        message: Human-readable summary suitable for log lines.
        counts: Free-form integer counters for telemetry (e.g.
            ``{"created": 12, "skipped": 3}``). Coarser than the legacy
            ``ComponentResult``'s 30+ fixed fields by design.
        warnings: Non-fatal warnings collected during the step.

    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    kind: Literal["success"] = "success"
    data: Any = None
    message: str = ""
    counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class Skipped(BaseModel):
    """A migration step that was intentionally skipped (no work to do).

    Attributes:
        kind: Discriminator literal — always ``"skipped"``.
        reason: Short, structured reason code (free-form string today,
            could become an enum later) — e.g. ``"no_work"``,
            ``"feature_disabled"``, ``"already_migrated"``.
        message: Optional longer human-readable explanation.

    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    kind: Literal["skipped"] = "skipped"
    reason: str = ""
    message: str = ""


class Failed(BaseModel):
    """A migration step that failed.

    Attributes:
        kind: Discriminator literal — always ``"failed"``.
        error: Primary error string (typically the first / most relevant
            failure). For single-shot steps this is the only error.
        message: Optional human-readable summary (the legacy envelope
            often used this for "Migration failed: 5 errors").
        errors: Full list of errors when a batched step collected
            multiple failures.
        counts: Partial counters captured before the failure — useful
            for diagnostics ("we created 8 of 10 before erroring").

    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    kind: Literal["failed"] = "failed"
    error: str = ""
    message: str = ""
    errors: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


# Discriminated union — pydantic v2 picks the right model from ``kind``.
StepResult = Annotated[
    Success | Skipped | Failed,
    Field(discriminator="kind"),
]


# ── Legacy bridge ────────────────────────────────────────────────────────
# These adapters let phase 8a land without touching any migration. As call
# sites are converted incrementally in later phases, the helpers below
# become the documented "how to bridge" for any code still on the legacy
# envelope.

# Heuristic prefix used to detect "no-op" legacy results. Migrations
# historically returned ``ComponentResult(success=True, message="No <X> ...")``
# to mean "nothing to do" — we reclassify those as ``Skipped`` so the
# discriminated union carries semantic intent rather than a magic string.
_SKIP_PREFIX = "No "


def _extract_counts(result: ComponentResult) -> dict[str, int]:
    """Pull the legacy envelope's per-domain integer counters into a dict.

    The legacy ``ComponentResult`` exposes ~20 named integer fields covering
    overlapping vocabulary (types/issues/fields). We surface only the ones
    that are non-zero so the resulting ``counts`` map stays compact.
    """
    candidates = (
        "total_types",
        "matched_types",
        "normalized_types",
        "created_types",
        "failed_types",
        "existing_types",
        "total_issues",
        "matched_issues",
        "normalized_issues",
        "created_issues",
        "failed_issues",
        "existing_issues",
        "success_count",
        "failed_count",
        "total_count",
        "extracted",
        "updated",
        "failed",
        "jira_fields_count",
        "op_fields_count",
        "mapped_fields_count",
    )
    counts: dict[str, int] = {}
    for name in candidates:
        value = getattr(result, name, 0)
        if isinstance(value, int) and value:
            counts[name] = value
    return counts


def from_component_result(result: ComponentResult) -> StepResult:
    """Convert a legacy ``ComponentResult`` into the discriminated union.

    The mapping rule is:

    * ``not result.success and (result.errors or result.error)`` → ``Failed``
      with the primary error sourced from ``result.error`` (or the first
      entry of ``result.errors``) and the full ``errors`` list preserved.
      Non-zero counters are forwarded as ``counts``.
    * ``result.success and result.message.startswith("No ")`` →
      :class:`Skipped`. This catches the common no-op pattern used across
      ~20 migrations (e.g. ``"No issues to migrate"``, ``"No custom fields
      found"``, ``"No work packages with attachments"``). The leading
      ``"No "`` prefix is treated as a heuristic, not a contract — callers
      that need precise classification should construct ``Skipped``
      directly.
    * Anything else (including ``success=True`` with warnings) → ``Success``
      with ``data``, ``message``, non-zero counts, and any ``warnings``.
      The discriminated union is intentionally coarser than
      ``ComponentResult``'s 30+ fields; counters that don't round-trip
      land in the free-form ``counts`` dict.

    Args:
        result: Legacy envelope returned by a migration component.

    Returns:
        One of :class:`Success`, :class:`Skipped`, or :class:`Failed`.

    """
    # Failure arm — explicit failure or a populated error field.
    if not result.success and (result.errors or result.error):
        primary = result.error or (result.errors[0] if result.errors else "")
        return Failed(
            error=primary,
            message=result.message,
            errors=list(result.errors),
            counts=_extract_counts(result),
        )

    # Skip heuristic — success + "No ..." message.
    if result.success and result.message.startswith(_SKIP_PREFIX):
        return Skipped(reason=result.message, message=result.message)

    # Default: success arm, including success-with-warnings.
    return Success(
        data=result.data,
        message=result.message,
        counts=_extract_counts(result),
        warnings=list(result.warnings),
    )


def to_component_result(result: StepResult) -> ComponentResult:
    """Convert a discriminated-union result back to legacy ``ComponentResult``.

    The legacy envelope has many fields; the round-trip is **lossy** in
    both directions because the discriminated union is intentionally
    coarser. This helper populates only the fields that the migration
    entry point and dashboard code actually inspect:

    * ``success``, ``message``, ``errors``, ``warnings``, ``error``, ``data``.

    Counters from a :class:`Success` or :class:`Failed` ``counts`` dict are
    written back to the matching named fields when they exist on
    ``ComponentResult``; unknown keys are dropped (the legacy envelope has
    no free-form counter store).

    Args:
        result: One of :class:`Success`, :class:`Skipped`, or :class:`Failed`.

    Returns:
        A populated :class:`ComponentResult` suitable for code paths that
        still expect the legacy shape.

    """
    # Local import to avoid a circular dependency between domain/ and models/.
    from src.models.component_results import ComponentResult

    if isinstance(result, Success):
        envelope = ComponentResult(
            success=True,
            message=result.message,
            data=result.data,
            warnings=list(result.warnings),
        )
        _apply_counts(envelope, result.counts)
        return envelope

    if isinstance(result, Skipped):
        # Skipped maps to success=True with the structured reason as the
        # message — this matches how legacy code treated "No work" results.
        return ComponentResult(
            success=True,
            message=result.message or result.reason,
        )

    # Failed
    envelope = ComponentResult(
        success=False,
        message=result.message,
        error=result.error or None,
        errors=list(result.errors),
    )
    _apply_counts(envelope, result.counts)
    return envelope


def _apply_counts(envelope: ComponentResult, counts: dict[str, int]) -> None:
    """Write ``counts`` keys onto matching ``ComponentResult`` integer fields.

    Unknown keys are silently dropped — the legacy envelope has a fixed
    schema and there is no escape hatch for arbitrary counters.
    """
    for name, value in counts.items():
        if hasattr(envelope, name) and isinstance(getattr(envelope, name), int):
            setattr(envelope, name, value)


__all__ = [
    "Failed",
    "Skipped",
    "StepResult",
    "Success",
    "from_component_result",
    "to_component_result",
]
