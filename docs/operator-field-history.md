# Operator-encrypted Jira field history

Preserve a Jira project's field changes as an age-encrypted attachment on an existing migrated work package. The operator who holds the separate private key can restore the original change values after Jira shutdown. Project members cannot read them with their OpenProject accounts alone. This archive is intended for recovery and subsequent verified conversion. It does not populate the native activity stream.

Export the project's issues locally from Jira with `expand=changelog` and the project field, saving the issue objects as a JSON array. Also save the response from `/rest/api/2/field`. Check the issue IDs against the project inventory. The command checks each issue's origin and each supplied changelog page's completeness. Only the supplied issues are in scope.

A new key can be generated in a private directory belonging to the source operator. Keep the identity and its backup separate from OpenProject. Losing it makes the archive unrecoverable.

```sh
mkdir -m 700 ~/jira-recovery-keys
uv run j2o-comments keygen --identity ~/jira-recovery-keys/history.agekey
uv run j2o-history seal --jira https://jira.example.org --project DEMO \
  --issues issues.json --fields fields.json --identity ~/jira-recovery-keys/history.agekey \
  --archive jira-DEMO-operator-field-history.age
```

Store the SHA-256 digest printed by `seal` in your trusted recovery records. Compare it with the downloaded file before recovery.

The archive retains issue and event IDs, timestamps, author keys, item positions and original before/after values. It includes recognized built-in changes and supported structured Agile and Tempo fields. For a reviewed standard custom field, pass its Jira ID with `--field-id`. Review its use before including it, especially for text fields. Unknown fields, comment fields and events or items with audience restrictions are skipped.

Select a work package from this export. Supply its numeric OpenProject project ID and the native custom-field IDs for the Jira origin ID and URL. Set `J2O_ARCHIVE_PUBLISHER_TOKEN` to the publisher's OpenProject credential through your usual secret store. When needed, set `J2O_ARCHIVE_CA_BUNDLE` to the path of your trusted CA bundle.

```sh
uv run j2o-history publish --openproject https://openproject.example.org \
  --project-id 7 --work-package 41 --origin-id-field 2 --origin-url-field 3 \
  --identity ~/jira-recovery-keys/history.agekey --archive jira-DEMO-operator-field-history.age
```

The publisher checks the private target project and the issue provenance, then downloads and compares the stored ciphertext. Repeating the same sealed snapshot reuses the existing attachment. A changed or duplicate attachment blocks publication. Keep one publisher for a given attachment.

For recovery, download the attachment from the work package and use the operator's original identity on a trusted machine. The restore command creates a new private JSON file and refuses to overwrite an existing file.

```sh
uv run j2o-history restore --archive jira-DEMO-operator-field-history.age \
  --identity ~/jira-recovery-keys/history.agekey --output restored-history.json
```
