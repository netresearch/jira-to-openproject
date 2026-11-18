# Retiring Jira with protected comments

`j2o-comments` stores reader-encrypted copies in one attachment named `jira-protected-comments.j2o` on each mapped OpenProject work package that has comments. Each reader can open exactly the view captured with their Jira account. The attachment contains age ciphertext. OpenProject database backups, ordinary project access and the OpenProject admin account alone cannot decrypt it. The reader uses the companion command to view original text, comment IDs, authors and dates. OpenProject search and the native activity stream do not index protected bodies.

## Freeze the source and prepare the destination

1. Complete the normal project and work-package migration on a fresh, closed target. Keep `J2O_COPY_PUBLIC_COMMENTS=0` for an archive-only migration. Earlier imports may have written unmarked, ordinary comments. Restore a clean pre-import target in that case.
2. Freeze Jira comments, memberships, issue security and account mappings until cutover. Inventory every person whose access you need to preserve, including customers, agents, users outside regular project groups and any historical accounts you intend to retain. Export the approved Jira user keys and mapped OpenProject user IDs to an independent JSON array, for example `[{"jira_key": "JIRAUSER10001", "op_id": 22}]`. This is the authority for checking that all intended readers enrolled. A single administrator's visible comments do not prove the roster is complete.
3. Use a Jira Server/Data Center inventory account that can read all in-scope issues and comments, a publisher with OpenProject attachment rights, and a personal Jira bearer token and OpenProject API token for each participant. The capture supports software and business projects and JSM customer requests. Service projects must expose consistent request and Jira comment inventories. A non-request issue in such a project, a disagreement about a reader between the two APIs, or an unrepresented comment blocks cutover. The target must serve attachment bytes via its authenticated content endpoint. A redirect to external object storage currently stops verification.
4. Install this distribution with `uv sync --frozen`. Serve both applications over trusted HTTPS. Configure a private CA with `J2O_ARCHIVE_CA_BUNDLE` if needed. Keep credential files, private keys and state in a private directory outside the source checkout and Docker build context. Restrict the migration config and mapping to operators. The state stores ciphertext and verification hashes. The snapshot covers one frozen project per state file.

## Enroll on each reader's device

Set `J2O_ARCHIVE_JIRA_TOKEN` and `J2O_ARCHIVE_OP_TOKEN` to the reader's own temporary credentials, obtained from the migration source and target respectively. Keep the identity file private, backed up and separate from OpenProject. Anyone with a copy of that secret key can read that user's archive. Losing every copy loses that user's private archive access. On Windows restrict the directory ACL to that user.

```sh
uv run j2o-comments keygen --identity reader.agekey
uv run j2o-comments enroll --identity reader.agekey \
  --jira https://jira.example.org --openproject https://op.example.org > enrollment.json
```

Return only the enrollment JSON over the organization's authenticated channel. Give the trusted migration operator the temporary API tokens via a secret manager. The operator requires these until final verification. The reader's secret age key stays on their device. Enrollment binds the public recipient, both base URLs and both actual account IDs to the same credentials. Swapped enrollment fields or duplicate account or public keys stop the run. Token rotation before cutover requires re-enrollment.

## Migrate and verify

Build a JSON config with the two base URLs, the numeric OpenProject custom-field IDs for **J2O Origin ID** and **J2O Origin URL**, and one entry per reader. Insert the actual enrollment object for each reader.

```json
{
  "jira": "https://jira.example.org",
  "openproject": "https://op.example.org",
  "origin_id_field": 42,
  "origin_url_field": 43,
  "readers": [{
    "enrollment": {"jira": "https://jira.example.org", "openproject": "https://op.example.org", "jira_key": "JIRAUSER10001", "op_id": 22, "recipient": "<public age recipient>", "jira_proof": "<from enrollment>", "op_proof": "<from enrollment>"},
    "jira_token_env": "CUTOVER_SOURCE_22",
    "op_token_env": "CUTOVER_TARGET_22"
  }]
}
```

Export the issue and comment IDs independently from the frozen source, including issues with zero comments. For Jira on PostgreSQL use a read-only account with the included query. It reads IDs only. Other databases can produce the same JSON, e.g. `{"jira":"https://jira.example.org","project":"DEMO","issues":{"10001":["3001","3002"],"10002":[]}}`. [Jira Data Center database reference for issue comments](https://support.atlassian.com/jira/kb/how-to-find-all-the-comments-related-to-one-user-in-the-database/)

```sh
psql -XqAt -v ON_ERROR_STOP=1 -v project_key=DEMO -v source_url=https://jira.example.org \
  -f contrib/jira-comment-inventory.sql > frozen-comment-ids.json
```

Set the reader tokens in those named environment variables, and set `J2O_ARCHIVE_INVENTORY_TOKEN` and `J2O_ARCHIVE_PUBLISHER_TOKEN` for the inventory and upload accounts. Use the normal migration's complete work-package mapping. Run one publisher per target and keep the state file for retries.

```sh
uv run j2o-comments migrate --project DEMO --config roster.json \
  --directory frozen-accounts.json --inventory frozen-comment-ids.json \
  --mapping var/data/work_package_mapping.json \
  --state private/DEMO.sqlite
```

The command reconciles issue IDs and keys, checks target provenance, pages through each actual reader's comments, accounts for every inventory comment, and checks work-package access. After collecting the complete project it uploads sealed files and downloads them as each eligible target account. A restart reuses the exact previously sealed bytes. Duplicate attachments, changed source grants or text, an unexpected target file, and failed read-backs stop the run. Keep access closed on any failure. If the frozen scope or grants changed, restore a clean pre-import target and rebuild the affected snapshots. Removing an attachment cannot retract files already downloaded.

Record the successful issue, distinct comment, reader-copy and archive counts per project. Reconcile these against the frozen directory and source inventory. Before switching off Jira, have representative users read back their own views with the source disconnected. Include two distinct reader groups on the same issue and an OpenProject admin without source access. Revoke the temporary migration tokens after acceptance. Reading requires only the personal decryption key and either a fresh OpenProject API token or the downloaded file.

## Read after cutover

```sh
uv run j2o-comments read --identity reader.agekey --work-package 245 \
  --openproject https://op.example.org
```

The online form uses `J2O_ARCHIVE_OP_TOKEN` for the current OpenProject reader. A user can also download the attachment in the work-package UI and run `uv run j2o-comments read --identity reader.agekey --work-package 245 --file jira-protected-comments.j2o`. Each form prints only that recipient's original comment text and metadata as JSON. The reader needs no Jira service, credential, or network connection to Jira. An archive is a cutover snapshot. Later target role grants cannot add recipients to existing ciphertext.

To repeat the disposable OpenProject 17.9.7 acceptance test locally, run `bash tests/integration/run_comment_cutover.sh` from the checkout with Docker and uv available. It creates and removes its own database and application containers. The test uses synthetic Jira and Service Management responses and authenticates each target account against OpenProject itself.
