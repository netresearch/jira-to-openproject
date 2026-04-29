"""Unit coverage for Rails console client error-marker handling.

The original live-Rails integration test required a tmux session named
``rails_console`` which isn't available in CI. This version exercises the
pure error-classification helpers so the same intent — distinguishing between
an ``ERROR_MARKER`` literal appearing in user output and a real Ruby error —
is verified without any external infrastructure.
"""

from src.clients.rails_console_client import RailsConsoleClient

# tests/unit/* is auto-marked `unit` by the conftest fixture; no pytestmark needed.


def test_error_marker_detection() -> None:
    """Literal marker words must not be mis-classified as fatal console errors."""
    # Harmless output containing an ``ERROR_MARKER`` literal — must not be
    # flagged as a fatal console error.
    benign = 'Command output: This is just a test ERROR_MARKER string\n"SUCCESS"'
    assert RailsConsoleClient._has_fatal_console_error(benign) is False

    # Output containing SUCCESS string — still benign.
    success_output = 'This output contains SUCCESS message\n"Completed successfully"'
    assert RailsConsoleClient._has_fatal_console_error(success_output) is False

    # A real console-level stack overflow *must* be flagged as fatal.
    fatal = "SystemStackError: stack level too deep\n\tfrom (irb):1:in `foo'"
    assert RailsConsoleClient._has_fatal_console_error(fatal) is True

    # Empty output is trivially non-fatal.
    assert RailsConsoleClient._has_fatal_console_error("") is False

    # ``_extract_error_summary`` should produce a non-empty summary for real
    # error output and behave gracefully on benign output.
    summary = RailsConsoleClient._extract_error_summary(fatal)
    assert "SystemStackError" in summary or "stack level too deep" in summary
