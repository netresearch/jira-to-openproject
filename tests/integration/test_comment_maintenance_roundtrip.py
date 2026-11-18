# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Exercise journal identity and audience checks against a disposable OpenProject."""

import hashlib

import pytest

from scripts.backfill_comment_provenance_markers import _build_fetch_journals_script, _build_update_markers_script
from scripts.cleanup_anonymous_comment_duplicates import _build_delete_script, _journal_snapshot
from tests.integration.test_openproject_agile_roundtrip import rails  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_journal_repairs_recheck_both_readerships_and_preserve_history(rails):  # noqa: F811
    client, read = rails
    execute = client.execute_query_to_json_file
    wp_id, kept, duplicate = read("""
      wp_id = Journal.where(journable_type: 'WorkPackage').group(:journable_id).having('count(*) >= 2').count.keys.first
      rows = Journal.where(journable_type: 'WorkPackage', journable_id: wp_id).order(:id)
      rows.update_all(notes: '', restricted: false)
      ids = rows.limit(2).ids
      rows.where(id: ids).update_all(notes: 'A public note')
      [wp_id, *ids]
    """)
    rows = {row["id"]: row for row in execute(_build_fetch_journals_script(wp_id))["journals"]}
    plan = _build_delete_script(
        [{"keeper": _journal_snapshot(rows[kept]), "candidate": _journal_snapshot(rows[duplicate]), "orphan": False}]
    )
    label = "<!-- j2o:jira-comment-id:12 -->"
    marker = {
        "id": duplicate,
        "wp_id": wp_id,
        "user_id": rows[duplicate]["user_id"],
        "marker": label,
        "digest": hashlib.sha256(b"A public note").hexdigest(),
    }
    mark = _build_update_markers_script([marker])
    read(f"Journal.find({kept}).update_columns(restricted: true, notes: 'Private fixture')")
    inventory = execute(_build_fetch_journals_script(wp_id))
    assert inventory["restricted_notes"]
    assert "Private fixture" not in str(inventory)
    assert execute(plan)["deleted"] == 0
    assert execute(mark)["updated"] == 0
    read(f"Journal.find({kept}).update_columns(restricted: false, notes: 'Edited')")
    assert execute(plan)["deleted"] == 0
    assert execute(mark) == {"updated": 1, "already_applied": 0}
    assert execute(mark) == {"updated": 0, "already_applied": 1}
    assert execute(_build_update_markers_script([marker | {"id": -1}]))["updated"] == 0
    assert read(f"Journal.find({duplicate}).notes") == "A public note\n" + label
    read(f"Journal.where(id: [{kept}, {duplicate}]).update_all(notes: 'A public note')")
    assert execute(plan)["deleted"] == 1
    assert read(f"Journal.find({kept}).notes") == "A public note"
    assert read(f"Journal.find({duplicate}).notes") == ""
