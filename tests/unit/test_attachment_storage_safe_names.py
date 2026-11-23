# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Attachment names that the Rails attach script refuses are stored under a safe name."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.components.attachment_recovery_migration import AttachmentRecoveryMigration
from src.application.components.attachments_migration import AttachmentsMigration, storage_safe_attachment_name


class _RecordingClient:
    """Answer each staged attachment with a new attachment id, as the Rails script does."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def execute_script_with_data(self, script_content: str, data: list[dict]) -> dict:
        self.sent = list(data)
        results = [
            {"jira_key": op["jira_key"], "filename": op["filename"], "attachment_id": 500 + index}
            for index, op in enumerate(data)
        ]
        return {"status": "success", "data": {"results": results, "errors": []}}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("License for 1234//, Invoice 77.msg", "License for 1234__, Invoice 77.msg"),
        ("report (2018-08-07 18;18;39).html", "report (2018-08-07 18_18_39).html"),
        ('WG: Test mail from site "Example Ltd"', "WG: Test mail from site _Example Ltd_"),
        ("back\\slash.txt", "back_slash.txt"),
        ("tab\there\x85.txt", "tab_here_.txt"),
        ("9:26.png", "9:26.png"),
        ("plain.pdf", "plain.pdf"),
    ],
)
def test_unsafe_characters_become_underscores(name: str, expected: str) -> None:
    assert storage_safe_attachment_name(name, "fallback") == expected


@pytest.mark.parametrize("name", ["", "   ", ".", ".."])
def test_a_name_without_a_usable_character_takes_the_fallback(name: str) -> None:
    assert storage_safe_attachment_name(name, "jira-attachment-7") == "jira-attachment-7"


def test_a_long_name_keeps_its_extension_within_255_bytes() -> None:
    name = "ä" * 200 + ".png"
    safe = storage_safe_attachment_name(name, "fallback")
    assert len(safe.encode()) <= 255
    assert safe.endswith(".png")
    assert safe.startswith("ää")


def test_every_character_the_rails_guard_refuses_is_replaced() -> None:
    # The guard is %r{[[:cntrl:]/\\;"]}: C0 and C1 controls, DEL and four punctuation marks.
    refused = [chr(code) for code in [*range(0x20), 0x7F, *range(0x80, 0xA0)]] + ["/", "\\", ";", '"']
    for char in refused:
        assert storage_safe_attachment_name(f"a{char}b", "fallback") == "a_b", repr(char)


def test_the_stored_name_is_safe_and_the_mapping_keeps_the_jira_name() -> None:
    client = _RecordingClient()
    migration = AttachmentsMigration(jira_client=SimpleNamespace(), op_client=client)  # type: ignore[arg-type]
    ops = [
        {"jira_key": "PRJ-1", "work_package_id": 12, "filename": "notes (18;18).html", "container_path": "/s/0"},
        {"jira_key": "PRJ-1", "work_package_id": 12, "filename": "notes (18_18).html", "container_path": "/s/1"},
    ]

    result = migration._attach_files(ops)

    assert result.success
    # The file whose own name is "notes (18_18).html" keeps it; the renamed file yields.
    assert [op["filename"] for op in client.sent] == ["notes (18_18) (2).html", "notes (18_18).html"]
    assert result.data["attachment_mapping"]["PRJ-1"] == {
        "notes (18_18) (2).html": 500,
        "notes (18;18).html": 500,
        "notes (18_18).html": 501,
    }


def test_a_duplicate_suffix_on_a_shortened_name_stays_within_255_bytes() -> None:
    client = _RecordingClient()
    migration = AttachmentsMigration(jira_client=SimpleNamespace(), op_client=client)  # type: ignore[arg-type]
    ops = [
        {"jira_key": "PRJ-1", "work_package_id": 12, "filename": "x" * 300 + ";.txt", "container_path": "/s/0"},
        {"jira_key": "PRJ-1", "work_package_id": 12, "filename": "x" * 300 + "_.txt", "container_path": "/s/1"},
    ]

    assert migration._attach_files(ops).success

    names = [op["filename"] for op in client.sent]
    assert len(set(names)) == 2
    assert all(len(name.encode()) <= 255 and name.endswith(".txt") for name in names)


def test_staged_file_names_stay_below_the_component_limit(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"data")
    transfers: list[Path] = []
    client = SimpleNamespace(transfer_file_to_container=lambda local, remote: transfers.append(remote))
    migration = AttachmentsMigration(jira_client=SimpleNamespace(), op_client=client)  # type: ignore[arg-type]
    op = {"jira_key": "PRJ-1", "work_package_id": 12, "filename": "y" * 255, "local_path": source.as_posix()}

    staged = migration._transfer_attachments([op], Path("/private/stage"))

    assert staged[0]["filename"] == "y" * 255
    assert len(transfers[0].name.encode()) <= 210


def test_the_recovery_audit_expects_the_stored_name() -> None:
    attachments = [
        SimpleNamespace(filename="a;b.html", id="1", size=3, content="https://jira.example/a"),
        SimpleNamespace(filename="a_b.html", id="2", size=3, content="https://jira.example/b"),
    ]

    entries = AttachmentRecoveryMigration._attachment_entries(
        SimpleNamespace(_read_attr=AttachmentRecoveryMigration._read_attr),  # type: ignore[arg-type]
        attachments,
    )

    assert [row["filename"] for row in entries] == ["a_b (2).html", "a_b.html"]
