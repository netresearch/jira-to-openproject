# Native completed sprints on OpenProject 17.9

Use this opt-in import after the project, work packages, fix versions and `sprint_epic` migration. It materializes completed Jira sprints in OpenProject Backlogs. The normal work-package sprint history retains every original membership in source order. The native sprint link uses the most recently completed membership, with the higher Jira sprint ID breaking timestamp ties. Existing fix versions and original work-package timestamps are preserved.

Use one manifest per frozen private target before adding project members. Enablement of the Backlogs module is part of the import. Sprint sharing must be off. The target project requires its migrated Jira base URL, project ID and key. Each participating work package requires its Jira origin ID, origin URL and `Jira sprint membership history` from the regular import.

Prepare the manifest from the same frozen Jira inventory as the work packages. For each board, collect **every** project key from the paginated `/rest/agile/1.0/board/{id}/project` response. Include all closed sprint resources from project-owned boards and any additional sprints referenced by this project's issues. Read their detail from `/rest/agile/1.0/sprint/{id}`. For each issue bearing sprint memberships, supply its source issue ID, key and the sprint IDs from the Jira Sprint field in their original order.

```json
{
  "jira": "https://jira.example.org",
  "project": {"id": "100", "key": "EX"},
  "targetProjectId": 7,
  "boardProjectKeys": {"6": ["EX"]},
  "sprints": [{
    "id": 20, "originBoardId": 6, "name": "January", "state": "closed", "goal": "",
    "startDate": "2026-01-01T09:00:00+01:00", "endDate": "2026-01-14T17:00:00+01:00",
    "activatedDate": "2026-01-01T09:12:00+01:00", "completeDate": "2026-01-14T16:45:00+01:00"
  }],
  "issues": [{"id": "501", "key": "EX-1", "sprintIds": [20]}]
}
```

A sprint on a shared board requires a matching ID and name in this project's imported issue history. Each target receives a project-local sprint. The importer currently supports closed sprints with all four timestamps and an empty goal. Other states, missing timestamps, nonempty goals, unknown scopes and conflicting assignments stop the transaction.

From a repository checkout, copy the script to the OpenProject application container. Use the container name for your target.

```sh
docker cp src/ruby/import_completed_sprints.rb "$OP_CONTAINER:/app/tmp/import_completed_sprints.rb"
docker exec -i "$OP_CONTAINER" bundle exec rails runner /app/tmp/import_completed_sprints.rb < completed-sprints.json
```

With an installed Python package, locate the bundled runner in the same environment where you installed the tool. Use this path for the source argument of `docker cp`.

```sh
sprint_runner=$(python -c 'from importlib.resources import files; print(files("src").joinpath("ruby", "import_completed_sprints.rb"))')
docker cp "$sprint_runner" "$OP_CONTAINER:/app/tmp/import_completed_sprints.rb"
```

The result reports native sprints, linked work packages, historical memberships and changes. Run the same manifest twice and check that the second result reports zero changes. The admin-only project attribute `J2O completed sprint origins` retains the source IDs, raw timestamps, membership IDs and target sprint IDs. The transaction checks the stored snapshot and native values on replay. Existing native sprints with matching names and unknown provenance require reconciliation before the first import.
