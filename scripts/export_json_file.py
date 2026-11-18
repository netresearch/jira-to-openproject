# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Publish a complete local JSON export in the operator-selected directory."""

if __package__:
    from .operator_files import write_operator_json as write_json_export
else:
    from operator_files import write_operator_json as write_json_export

__all__ = ["write_json_export"]
