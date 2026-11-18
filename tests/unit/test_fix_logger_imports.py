# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import ast

from scripts.fix_logger_imports import _rewrite


def test_logger_rewrite_preserves_python_header_and_unrelated_uses():
    original = (
        '#!/usr/bin/python3\r\n"""logger = config.logger"""\r\n'
        "from __future__ import annotations\r\nfrom src import config\r\n"
        "logger = config.logger  # current\r\nother = config.settings\r\n"
    )
    updated = _rewrite(original)
    assert updated == original.replace(
        "logger = config.logger  # current",
        'from src.display import configure_logging\r\nlogger = configure_logging("INFO", None)  # current',
    )
    assert _rewrite(updated) == updated
    assert ast.get_docstring(ast.parse(updated)) == "logger = config.logger"
