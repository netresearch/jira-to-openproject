# Retiring Jira with protected comments

`j2o-comments` stores reader-encrypted copies in one attachment named `jira-protected-comments.j2o` on each mapped OpenProject work package that has comments. Each reader can open exactly the view captured with their Jira account. The attachment contains age ciphertext. OpenProject database backups, ordinary project access and the OpenProject admin account alone cannot decrypt it. The reader uses the companion command to view original text, comment IDs, authors and dates. OpenProject search and the native activity stream do not index protected bodies.

## Freeze the source and prepare the destination

1. Complete the normal project and work-package migration on a fresh, closed target. Keep `J2O_COPY_PUBLIC_COMMENTS=0` for an archive-only migration. Earlier imports may have written unmarked, ordinary comments. Restore a clean pre-import target in that case.
2. Freeze Jira comments, memberships, issue security and account mappings until cutover. Inventory every person whose access you need to preserve, including customers, agents, users outside regular project groups and any historical accounts you intend to retain. Export the approved Jira user keys and mapped OpenProject user IDs to an independent JSON array, for example `[{"jira_key": "JIRAUSER10001", "op_id": 22}]`. This is the authority for checking that all intended readers enrolled. A single administrator's visible comments do not prove the roster is complete.
3. Use a Jira Server/Data Center inventory account that can read all in-scope issues and comments, a publisher with OpenProject attachment rights, and a personal Jira bearer token and OpenProject API token for each participant. The capture supports software and business projects and JSM customer requests. Service projects must expose consistent request and Jira comment inventories. A non-request issue in such a project, a disagreement about a reader between the two APIs, or an unrepresented comment blocks cutover. If OpenProject restricts attachment extensions, allow the j2o extension for the archive file. The target must serve attachment bytes via its authenticated content endpoint. A redirect to external object storage currently stops verification.
4. Install this distribution with `uv sync --frozen`. Serve both applications over trusted HTTPS. Configure a private CA with `J2O_ARCHIVE_CA_BUNDLE` if needed. Keep credential files, private keys and state in a private directory outside the source checkout and Docker build context. Restrict the migration config and mapping to operators. The state stores ciphertext and verification hashes. The snapshot covers one frozen project per state file.

## Enroll on each reader's device

Choose a private directory outside the checkout (shown as `/secure/jira-cutover` below). Set `J2O_ARCHIVE_JIRA_TOKEN` and `J2O_ARCHIVE_OP_TOKEN` to the reader's own temporary credentials, obtained from the migration source and target respectively. Keep the identity file private, backed up and separate from OpenProject. Anyone with a copy of that secret key can read that user's archive. Losing every copy loses that user's private archive access. On Windows restrict the directory ACL to that user.

```sh
umask 077
archive_dir=/secure/jira-cutover
mkdir -p "$archive_dir"
uv run j2o-comments keygen --identity "$archive_dir/reader.agekey"
uv run j2o-comments enroll --identity "$archive_dir/reader.agekey" \
  --jira https://jira.example.org --openproject https://op.example.org > "$archive_dir/enrollment.json"
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
  -f contrib/jira-comment-inventory.sql > "$archive_dir/frozen-comment-ids.json"
```

Set the reader tokens in those named environment variables, and set `J2O_ARCHIVE_INVENTORY_TOKEN` and `J2O_ARCHIVE_PUBLISHER_TOKEN` for the inventory and upload accounts. Supply the normal migration's complete work-package mapping. If that file is missing, the publisher recovers a candidate mapping from the visible target work packages and checks each origin ID and URL against both frozen inventories. Ambiguous or incomplete provenance blocks publication. Run one publisher per target and keep the state file for retries.

```sh
uv run j2o-comments migrate --project DEMO --config "$archive_dir/roster.json" \
  --directory "$archive_dir/frozen-accounts.json" --inventory "$archive_dir/frozen-comment-ids.json" \
  --mapping var/data/work_package_mapping.json \
  --state "$archive_dir/DEMO.sqlite"
```

The command reconciles issue IDs and keys, checks target provenance, pages through each actual reader's comments, accounts for every inventory comment, and checks work-package access. After collecting the complete project it uploads sealed files and downloads them as each eligible target account. A restart reuses the exact previously sealed bytes. Duplicate attachments, changed source grants or text, an unexpected target file, and failed read-backs stop the run. Keep access closed on any failure. If the frozen scope or grants changed, restore a clean pre-import target, freeze the revised scope and rebuild with a new state file. Keep the original state for unchanged retries only. Removing an attachment cannot retract files already downloaded.

Record the successful issue, distinct comment, reader-copy and archive counts per project. Reconcile these against the frozen directory and source inventory. Before switching off Jira, have representative users read back their own views with the source disconnected. Include two distinct reader groups on the same issue and an OpenProject admin without source access. Revoke the temporary migration tokens after acceptance. Reading requires only the personal decryption key and either a fresh OpenProject API token or the downloaded file.

## Read after cutover

```sh
uv run j2o-comments read --identity "$archive_dir/reader.agekey" --work-package 245 \
  --openproject https://op.example.org
```

The online form uses `J2O_ARCHIVE_OP_TOKEN` for the current OpenProject reader. A user can also download the attachment in the work-package UI and run `uv run j2o-comments read --identity "$archive_dir/reader.agekey" --work-package 245 --file jira-protected-comments.j2o`. Each form prints only that recipient's original comment text and metadata as JSON. The reader needs no Jira service, credential, or network connection to Jira. An archive records the source audience at capture. A later Jira group grant or OpenProject role grant cannot add a reader to an existing encrypted file. A former member who kept an authorized copy and its private key can still decrypt it after removal. To capture a revised audience before cutover, repeat the independent inventory and freeze, then publish from new state to a clean target as described above.

To repeat the disposable OpenProject 17.9.7 acceptance test locally, run `bash tests/integration/run_comment_cutover.sh` from the checkout with Docker and uv available. It creates and removes its own database and application containers. The suite uses synthetic Jira and Service Management responses and authenticates each target account against OpenProject itself. It also checks the ordinary journal importer on a private work package, group membership changes, and the stored work-package fields and relationships after a repeat import.

The group-change test follows one ordinary account, one work package and the same personal key through three captures. Before joining the source group it reads the public comment. After joining it reads both public and restricted comments. After leaving it reads only the public comment from the new archive. The test also decrypts the earlier authorized copy offline and confirms that reusing the old migration state rejects the changed audience. The automated source fixture filters comments against its current group membership; account authentication, attachment access and export on the target run through OpenProject.

For the same test against Jira Software 9.12.3, use a disposable `atlassian/jira-software:9.12.3` container named `j2o-grptest-<run>-jira` on a single internal Docker network named `j2o-grptest-<run>`. The test connects directly to its private bridge address from the Docker host. Enable group comment visibility in Jira’s general configuration. Create project `GROUP`, one issue with comments `Fixture public comment` and `Fixture group-only comment`, and restrict the latter to `fixture-reviewers`. Grant project browse access to two ordinary test accounts. Add the persistent reader and the inventory account to the group. Name the switching account `groupverifierreader`. The inventory account also needs permission to manage this test group. A short-lived host license is available for developer testing. [Atlassian test licenses](https://developer.atlassian.com/platform/marketplace/timebomb-licenses-for-testing-server-apps/)

Set `J2O_TEST_GROUP_JIRA_FIXTURE` to a private JSON file containing `container`, `issue`, `issue_id`, `public_id`, `protected_id`, `inventory_token`, and two dictionaries keyed by `anchor` and `reader`. The `principals` dictionary holds the independently checked Jira user keys, and `tokens` holds those users’ personal bearer tokens. All issue and comment IDs are decimal strings. Run `bash tests/integration/run_comment_cutover.sh` from a host with access to this internal Docker network. The test adds and removes only the named synthetic reader. Destroy the disposable source and its tokens after testing.

## Accept with copies of real comments

Before Jira is switched off, repeat the reader check with copies of real company comments on a disposable local Jira Software 9.12.3 and OpenProject 17.9.7. The source is only read. The fixture and all results stay in a private directory outside the checkout.

1. Select the sample with a read-only Jira token: `J2O_SAMPLE_JIRA_TOKEN=… uv run python -m tests.integration.company_comment_sample --jira https://jira.example.org --output "$private/fixture.json" --exclude-prefix <PREFIX>`. It picks the smallest issue with only ordinary comments, with a group-restricted comment, with a project-role-restricted comment and with an issue security level. `--exclude-project` and `--issue case=KEY` narrow or pin the choice. The script refuses an output directory that other users can access and replaces the fixture with a new owner-only file.
2. Run `bash tests/integration/run_company_comment_acceptance.sh up`, then `… test` and `… down`, with `J2O_COMPANY_COMMENT_FIXTURE` set to the fixture, `J2O_COMPANY_COMMENT_STATE` set to a private directory outside the checkout, and `J2O_ACCEPT_JIRA_LICENSE` set to a file with a 10-user Jira Software Data Center test license. The script serves both applications over HTTPS with a private CA on `127.0.0.1` ports 18443 and 18444 (`J2O_ACCEPT_JIRA_PORT`, `J2O_ACCEPT_OP_PORT`).
3. The test recreates each sample in its own project with one authorized and one unauthorized ordinary account. Comment bodies and visibility are copied; Jira assigns new comment IDs, the loader account as author and the current date. It then runs `j2o migrate --profile full` for these projects with `J2O_COPY_PUBLIC_COMMENTS=0`, publishes each project's archive with `j2o-comments` and reads every view with each account's own Jira token, OpenProject API token and age key. It also checks anonymous access and the ordinary activity stream. The runner prints counts only.

j2o writes its data and logs below the checkout's `var/`; the test points `var` at the private state directory for the run and stops if the checkout already holds a `var/` directory, which must then be moved aside first. The test is skipped unless both `J2O_COMPANY_COMMENT_FIXTURE` and `J2O_ACCEPT_STACK` are set; CI does not run it.
