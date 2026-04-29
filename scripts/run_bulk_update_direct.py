#!/usr/bin/env python3
"""Direct bulk update via SSH - bypasses tmux session issues.

This script transfers data via stdin to the rails runner for reliable execution.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.bulk_update_wp_metadata import build_lookup_tables, prepare_update_batch

BATCH_SIZE = 500
SSH_HOST = "sobol.nr"
CONTAINER = "openproject-web-1"

RUBY_SCRIPT = """
require 'json'
require 'time'

updates = JSON.parse(STDIN.read)
results = {updated: 0, skipped: 0, errors: []}

ActiveRecord::Base.transaction do
  updates.each do |upd|
    begin
      wp = WorkPackage.find_by(id: upd["op_id"])
      next results[:skipped] += 1 unless wp

      attrs = {}
      attrs[:priority_id] = upd["fields"]["priority_id"] if upd["fields"]["priority_id"]
      attrs[:author_id] = upd["fields"]["author_id"] if upd["fields"]["author_id"]
      attrs[:assigned_to_id] = upd["fields"]["assigned_to_id"] if upd["fields"]["assigned_to_id"]
      attrs[:created_at] = Time.parse(upd["fields"]["created_at"]) if upd["fields"]["created_at"]
      attrs[:updated_at] = Time.parse(upd["fields"]["updated_at"]) if upd["fields"]["updated_at"]

      if attrs.any?
        wp.update_columns(attrs)
        results[:updated] += 1
      else
        results[:skipped] += 1
      end
    rescue => e
      results[:errors] << {op_id: upd["op_id"], error: e.message}
    end
  end
end

puts results.to_json
"""


def run_batch_update(updates: list[dict], batch_num: int, total_batches: int) -> dict:
    """Run a batch update via SSH/rails runner with stdin data."""
    if not updates:
        return {"updated": 0, "skipped": 0, "errors": []}

    json_data = json.dumps(updates)

    try:
        result = subprocess.run(
            [
                "ssh",
                SSH_HOST,
                f"docker exec -i {CONTAINER} bundle exec rails runner '{RUBY_SCRIPT}'",
            ],
            check=False,
            input=json_data,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode == 0:
            try:
                return json.loads(result.stdout.strip().split("\n")[-1])
            except json.JSONDecodeError:
                return {"updated": 0, "skipped": 0, "errors": [{"error": f"Parse error: {result.stdout[-200:]}"}]}
        else:
            return {"updated": 0, "skipped": 0, "errors": [{"error": result.stderr[:500]}]}

    except subprocess.TimeoutExpired:
        return {"updated": 0, "skipped": 0, "errors": [{"error": "Timeout"}]}
    except Exception as e:
        return {"updated": 0, "skipped": 0, "errors": [{"error": str(e)}]}


def main():
    print("Loading mappings...")
    lookups = build_lookup_tables(Path("var/data"))

    jira_metadata = lookups["jira_metadata"]
    jira_key_to_op_id = lookups["jira_key_to_op_id"]

    all_keys = [k for k in jira_metadata.keys() if k in jira_key_to_op_id]
    print(f"Found {len(all_keys)} work packages to update")

    total_updated = 0
    total_skipped = 0
    total_errors = []
    total_batches = (len(all_keys) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(all_keys), BATCH_SIZE):
        batch_num = i // BATCH_SIZE + 1
        batch_keys = all_keys[i : i + BATCH_SIZE]

        print(f"Processing batch {batch_num}/{total_batches} ({len(batch_keys)} WPs)...", end=" ", flush=True)

        updates = prepare_update_batch(lookups, batch_keys)
        result = run_batch_update(updates, batch_num, total_batches)

        total_updated += result.get("updated", 0)
        total_skipped += result.get("skipped", 0)
        if result.get("errors"):
            total_errors.extend(result["errors"])

        print(
            f"updated={result.get('updated', 0)}, skipped={result.get('skipped', 0)}, errors={len(result.get('errors', []))}",
        )

        # Save progress
        progress = {
            "batch": batch_num,
            "total_batches": total_batches,
            "updated": total_updated,
            "skipped": total_skipped,
            "errors": len(total_errors),
        }
        with open("var/data/bulk_update_progress.json", "w") as f:
            json.dump(progress, f)

    print("\n" + "=" * 60)
    print("BULK UPDATE COMPLETE")
    print("=" * 60)
    print(f"Total updated: {total_updated}")
    print(f"Total skipped: {total_skipped}")
    print(f"Total errors: {len(total_errors)}")

    # Save final results
    with open("var/data/bulk_update_results.json", "w") as f:
        json.dump(
            {
                "updated": total_updated,
                "skipped": total_skipped,
                "errors": total_errors[:100],  # First 100 errors only
            },
            f,
            indent=2,
        )


if __name__ == "__main__":
    main()
