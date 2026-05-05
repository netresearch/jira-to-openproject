"""Unit coverage for the output filter that strips Rails console noise.

When the persistent Rails console executes a multi-line wrapped script
(``begin / rescue / ensure / end``), tmux echoes every input line back
with an irb continuation prompt — e.g. ``open-project(prod):2515* begin``.
Those echoes show up in the captured pane between ``--EXEC_START--`` and
``--EXEC_END--`` and must be filtered along with the sentinel lines, so
that callers parsing the actual script output (e.g. ``int(output)`` for
a custom-field id) see only that output and nothing else.

This regressed in the live ``J2O_SCRIPT_LOAD_MODE=console`` migration run
on 2026-05-04, taking out the ``resolutions``, ``security_levels``,
``affects_versions`` and ``votes_reactions`` components with
``ValueError: invalid literal for int()`` on prompt-prefixed echoes.
"""

from src.infrastructure.openproject.rails_console_client import (
    filter_console_output_lines,
)


def test_strips_irb_continuation_prompts_from_wrapped_script() -> None:
    r"""Prompt-prefixed echoes from a wrapped multi-line script must not leak.

    Reproduces the production trace where the wrapper
    ``begin\n  load '/tmp/.../runner_xxx.rb'\nrescue => e\n  ...\nend``
    was echoed back line-by-line with continuation prompts and ended up in
    the value passed to ``int()``.
    """
    between_lines = [
        "open-project(prod):2515* begin",
        "open-project(prod):2516*   load '/tmp/j2o_runner_3fde7c72.rb'",
        "open-project(prod):2517*   rescue => e",
        "open-project(prod):2518*   end",
        "27",
        "=> nil",
    ]
    assert filter_console_output_lines(between_lines) == "27"


def test_strips_exec_markers() -> None:
    between_lines = [
        "--EXEC_START--abcd",
        "hello",
        "--EXEC_END--abcd",
    ]
    assert filter_console_output_lines(between_lines) == "hello"


def test_strips_irb_main_prompts() -> None:
    between_lines = [
        "irb(main):001>  some_method_call",
        "actual_value",
    ]
    assert filter_console_output_lines(between_lines) == "actual_value"


def test_strips_arrow_autoprint_lines() -> None:
    """``=> nil`` and similar irb auto-print lines are noise when the real
    result is emitted via ``puts`` (which all our wrapper templates do).
    """
    between_lines = [
        "=> nil",
        "27",
        "=> 27",
    ]
    assert filter_console_output_lines(between_lines) == "27"


def test_strips_empty_and_whitespace_lines() -> None:
    between_lines = ["", "  ", "value", "\t"]
    assert filter_console_output_lines(between_lines) == "value"


def test_preserves_multiple_content_lines() -> None:
    between_lines = ["line one", "line two", "line three"]
    assert filter_console_output_lines(between_lines) == "line one\nline two\nline three"


def test_strips_prompt_from_mixed_input() -> None:
    """The realistic mixed case: prompts + markers + real output + auto-print."""
    between_lines = [
        "--EXEC_START--xyz",
        "open-project(prod):2515* begin",
        "open-project(prod):2516*   cf.id",
        "open-project(prod):2517* end",
        "42",
        "=> 42",
        "--EXEC_END--xyz",
    ]
    assert filter_console_output_lines(between_lines) == "42"


def test_returns_empty_string_when_only_noise() -> None:
    between_lines = [
        "--EXEC_START--xyz",
        "open-project(prod):1*  foo",
        "=> nil",
        "--EXEC_END--xyz",
    ]
    assert filter_console_output_lines(between_lines) == ""
