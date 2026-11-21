# Carry references into a later project cutover

An issue in the selected projects can link to an issue that will be migrated later. Links between issues migrated in the same run are created by the `relations` stage of `j2o migrate`. `j2o-references` stays a separate operator command because it needs the operator's private age identity and a later cutover wave. At a frozen Jira cutover, capture those edges from the local full-field Jira issue snapshot and all available work-package mappings. Keep the operator identity in private recovery storage. A separate identity for this purpose can be generated with `j2o-comments keygen`.

```sh
j2o-comments keygen --identity /private/recovery/ref.agekey
j2o-references --identity /private/recovery/ref.agekey --archive APP.age \
  --mapping var/data/work_package_mapping.json capture --issues full-fields.json \
  --project APP --jira https://jira.example --epic-field customfield_10008
```

Use the site's actual Jira Epic Link field ID. The issue file can be a list of raw Jira issues or a key-indexed cache. Each selected mapped issue must be present with its link list and its explicitly selected epic field, including null values. Repeat `--mapping` for additional mapping files. Input mappings use numeric Jira issue IDs with `jira_key` and `openproject_id`. A Jira epic field sometimes contains only an issue key. Supply an independently checked `--known-ids` JSON object mapping such keys to their immutable Jira IDs for later native replay.

The encrypted archive retains source and target identities, original link ID, type vocabulary, direction and the epic or direct-parent information. Linked issue contents and personal fields are omitted. The file can be attached to a private migration record in OpenProject and downloaded after Jira shutdown. The same operator identity authenticates the archive before replay. Test key recovery from separate storage. Repeating capture accepts an identical existing snapshot. A changed snapshot needs a new filename.

To read all authenticated reference records offline, run `j2o-references --identity /private/recovery/ref.agekey --archive APP.age read` in a private terminal.

After importing the remaining projects, download the archive, combine the latest mappings and run the following command against the configured OpenProject cutover instance with its projects still private and without members.

```sh
j2o-references --identity /private/recovery/ref.agekey --archive APP.age \
  --mapping var/data/work_package_mapping.json apply
```

The command verifies both native work packages against their Jira URL, key, system and immutable ID in the write transaction. It reuses existing matching native relations and epic parents. An existing different parent aborts the transaction. Missing mappings, unresolved target IDs, unknown link types and epics superseded by direct parents stay in the archive. A mapping file with entries of level work packages (`security_level_work_package_mapping.json`) is refused: a relation or parent across a level sub-project would show readers of the other end a placeholder row ([Level work packages](operator-restricted-issues.md#level-work-packages)); links inside one sub-project are created by `relations`. The output reports confirmed, newly created and held edges. Retain the snapshot so later projects can resolve the remaining edges.
