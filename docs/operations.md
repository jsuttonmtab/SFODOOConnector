# Operations: sandbox sync on mpl-odoo

> **Interim setup.** This runs the connector against the sandbox Odoo on mpl-odoo. It stays in place
> until two decisions are made:
> 1. **Cloud Odoo**: where production Odoo runs, and whether the connector moves with it.
> 2. **Salesforce integration user**: the connector logs in with the OAuth client-credentials flow
>    when `SF_CLIENT_ID` and `SF_CLIENT_SECRET` are set. Without them it falls back to the CLI login
>    below. The server uses OAuth from 19.0.1.8.10 on; the CLI login stays as a fallback.

## What runs where

| Piece | Where |
|---|---|
| Odoo 19 sandbox (`OdooSandbox19`) | mpl-odoo, port 8069 |
| Connector code | `/opt/sfsync/SFODOOConnector` |
| Connector settings | `/opt/sfsync/SFODOOConnector/connector/.env` (git-ignored) |
| Scheduled run | cron, as the `sfsync` user, every 5 minutes |
| Wrapper script | `/opt/sfsync/run_sync.sh` (copied from `deploy/run_sync.sh` in the repo) |
| Lock file | `/opt/sfsync/sync.lock` |
| Logs | `/opt/sfsync/logs/sync-YYYYMMDD.log` |
| Salesforce login | `sf` CLI login for alias `mtab-sandbox`, owned by `sfsync` |

## Server tooling (mpl-odoo)

- Python 3.14 (`python3`)
- Node 22, with npm from apt
- Salesforce CLI 2.155.8, installed globally with npm

The wrapper sets `PATH=/usr/local/bin:/usr/bin:/bin`, so cron finds `sf`.

## The sfsync account

- A dedicated Linux user, `sfsync`, runs everything. It does not log in interactively and
  should not have sudo.
- Home directory: `/opt/sfsync`. The repo, the logs, the lock file, the sf CLI login and the
  SSH key all live under it.

## GitHub deploy key (read-only)

The repo is cloned over SSH with a read-only deploy key, not a personal key.

- Key location: `/opt/sfsync/.ssh/id_ed25519`
- GitHub deploy key title: **mpl-odoo sfsync**. It is read-only (write access not granted).
- Clone URL: `git@github.com:jsuttonmtab/SFODOOConnector.git`. This is a personal GitHub
  account, not an organisation.

If the key ever has to be replaced: generate a new ed25519 key without a passphrase as
`sfsync`, add the `.pub` file in GitHub under **Settings → Deploy keys** with write access left
unticked, delete the old key there, and make sure `~/.ssh/config` uses the new file.

## connector/.env

`connector/.env` is git-ignored and lives on the server only. It is created from
`connector/.env.example`. The connector runs on the same machine as Odoo, so the URL is
`localhost`:

```
ODOO_URL=http://localhost:8069
ODOO_DB=OdooSandbox19
ODOO_API_KEY=<API key of the integration user in Odoo>
ODOO_PUBLIC_URL=<URL users open Odoo at, for the links on Salesforce records>
SF_LOGIN_URL=<My Domain login URL of the sandbox>
SF_CLIENT_ID=<consumer key of the External Client App "Odoo Integration">
SF_CLIENT_SECRET=<consumer secret of the same app>
SF_EXPECTED_ORG_ID=<optional: 18-character org Id a real run must match; set it for production>
SF_REQUIRE_SANDBOX=true
SF_WRITEBACK=false
SF_TARGET_ORG=mtab-sandbox
FINANCE_ACTIVITY_USER=<Odoo login of the Finance user>
DELIVERABLES_PROJECT=DS deliverables
ODOO_API_LOGIN=<Odoo login the API key belongs to; optional, used only for a warning>
AUTO_CREATE_PRODUCTS=false
```

`SF_CLIENT_SECRET` is a secret: keep `.env` out of git and out of chat, logs and screenshots.

- The API key comes from Odoo under Preferences → Account Security → New API Key, for the
  integration user. Don't use a personal user's key.
- Keep the file readable by `sfsync` only: `chmod 600 connector/.env`.
- `DELIVERABLES_PROJECT` (default `DS deliverables`) is the exact name of the active, billable Odoo project that connector-managed orders are set to. It must exist in Odoo, or the sync logs one warning and skips setting projects.
- `AUTO_CREATE_PRODUCTS=true` creates new service products in Odoo for Salesforce products
  with no match. Set it deliberately.

## Salesforce CLI login (imported, not created on the server)

The server has no browser, so the login is copied from the workstation as an sfdx auth URL.

**Export (on the workstation):**

1. `sf org auth show-sfdx-auth-url --target-org mtab-sandbox`. The CLI asks for confirmation
   before printing the URL.
   `sf org display --verbose` does **not** show it: in CLI 2.155.x the Sfdx Auth Url is shown
   as `[REDACTED]`.
2. Save the URL to a file and copy the file to the server with `scp`.
3. Delete the local file once it has been imported.

**Import (on the server, as `sfsync`):**

```
sf org login sfdx-url --sfdx-url-file <file> --alias mtab-sandbox
```

Then delete the file on the server. The file was deleted on both sides after the import.

**Check:** `sf org display --target-org mtab-sandbox --json` shows the instance URL and the
sandbox username.

Rules for this login:

- The auth URL contains a refresh token. Anyone who has it can use the login. Don't paste it
  into chat, tickets or commits.
- **Don't run `sf org logout --target-org mtab-sandbox` on the workstation.** The server shares
  the same login. Logging out on either machine breaks the sync on both.
- The login has the same rights as the Salesforce user who created it.

## /opt/sfsync/run_sync.sh

The cron job calls this wrapper. The source is `deploy/run_sync.sh` in the repo. The copy on the server
is installed from it (see *Installing the wrapper*), and it must be refreshed after a `git pull` that changes
the file. It:

1. **Locks**: takes `flock -n` on `/opt/sfsync/sync.lock`. If the previous run still holds
   the lock, the new run is skipped and the skip is logged, so runs never overlap.
2. **Sets PATH** to `/usr/local/bin:/usr/bin:/bin`, so `sf` is found.
3. **Runs**, from the repo directory:

   ```
   python3 -m connector.sync_accounts
   python3 -m connector.sync_orders
   ```

   Order lines are part of the order sync.
4. **Logs**: appends the output of both steps to `/opt/sfsync/logs/sync-YYYYMMDD.log`, one
   file per day.
5. **Retains 14 days** of logs.

`--dry-run` is not used by the wrapper. Run it by hand when checking a change.

### Installing the wrapper

As root, after the repo is at the commit you want:

```
sudo install -o sfsync -g sfsync -m 750 /opt/sfsync/SFODOOConnector/deploy/run_sync.sh /opt/sfsync/run_sync.sh
sudo diff /opt/sfsync/SFODOOConnector/deploy/run_sync.sh /opt/sfsync/run_sync.sh && echo same
```

Repeat the `install` after every `git pull` that changes `deploy/run_sync.sh`. The `diff` line should print `same`.

## Cron

In the `sfsync` user's crontab (`crontab -e` as `sfsync`):

```
*/5 * * * * /opt/sfsync/run_sync.sh
```

Every 5 minutes. Because of the lock, a run that is still going when the next one starts makes that
one skip (logged as `skipped: a run or a manual sync holds the lock`). Runs never overlap.

**Check it ran:**

```
sudo -u sfsync grep -E '^=== |skipped' /opt/sfsync/logs/sync-$(date +%Y%m%d).log | tail -n 6
sudo -u sfsync flock -n /opt/sfsync/sync.lock true && echo "lock free" || echo "lock held"
```

**Disable it quickly:** comment out the line in the `sfsync` crontab:

```
sudo -u sfsync bash -c 'crontab -l | sed "s|^\*/5|#*/5|" | crontab -'
```

Check with `sudo crontab -u sfsync -l`. Remove the `#` to turn it back on.

**Manual syncs while the cron is on:** run them on the server under the same lock, so the cron run
waits or skips:

```
sudo -u sfsync flock -w 900 /opt/sfsync/sync.lock bash -c 'cd /opt/sfsync/SFODOOConnector && python3 -m connector.sync_orders --opportunity <SF opportunity Id> --dry-run'
```

Do not run manual syncs from a workstation while the cron is on: the lock file is on the server only, so
a workstation run cannot see the cron run, and both write to the same Odoo and Salesforce sandbox.

## Pulling code updates

As `sfsync`:

```
cd /opt/sfsync/SFODOOConnector
git pull --ff-only
```

- `--ff-only` refuses to merge, so a local change on the server stops the pull instead of
  silently mixing in.
- The connector has no build step. The next cron run uses the new code.
- If the change includes the Odoo module (`odoo_addons/sf_connector`), also upgrade it in
  Odoo: Apps → sf_connector → Upgrade. Code pulled without the upgrade can fail when it writes
  to fields the database doesn't have yet.

## Upgrade to sf_connector 19.0.1.6.0 (Finance review)

Order matters. Upgrade the Odoo module first, then pull the code, before the next cron tick. The new
connector code writes `sf_cancelled_by_sync` and `sf_product_id`, which exist only after the upgrade.
(The reverse order, new module with old code, is harmless: the new fields are optional.)

1. In Odoo (mpl-odoo sandbox first, then production): Apps → sf_connector → **Upgrade**. Watch the log
   for errors on the views `sale_order_list_search_views.xml` and `product_template_views.xml` (see below).
   Or from the shell, as the Odoo service user: `odoo -d <database> -u sf_connector --stop-after-init`.
2. The upgrade runs `migrations/19.0.1.6.0/post-migrate.py`: the existing **Net 45 PSA** term gets the new
   definition (45 days after end of month) in place. Check it under Accounting → Payment Terms: one
   "Net 45 PSA", line "45 days after end of month".
3. As `sfsync`: `cd /opt/sfsync/SFODOOConnector && git pull --ff-only`.
4. `python3 -m connector.check_connections`: both connections must log in.
5. Check the next run's log: `Products matched by Id`, `matched by name (stamped)`, `created`, `missing`
   in the `Done` line; `quotations cancelled` and `reopened`; `NOT written` if any.

Views to check on the first upgrade: the order and quotation list ids and the search id in
`sale_order_list_search_views.xml`, and the product form's `notebook` in `product_template_views.xml`,
were written without a running Odoo 19 to check against. If an upgrade fails on one of them, fix that
xpath or view id; the rest of the module does not depend on it.

## Upgrade to sf_connector 19.0.1.8.1 (invoice PO number fix)

1. Odoo: Apps → sf_connector → **Upgrade** (the invoice report template changed). No migration.
2. `git pull --ff-only`, then `python3 -m connector.check_connections`.
3. The connector's description lock is now a warning, not an error: a line with a changed description on an invoiced
   line shows `WARNING description not updated (line is delivered or invoiced)` once per run.
4. Set `SF_DELIVERED_FIELD=Fulfillment_Quantity__c` in the server's `.env` (decided 2026-11-18, docs/field-mapping.md
   §5.1a). The counter is never lowered; lines where Odoo is lower than Salesforce are logged as warnings.

## Upgrade to sf_connector 19.0.1.8.0 (information-only lines, description field, invoice layout)

Order: upgrade the Odoo module first, then pull the code.

1. Odoo: Apps → sf_connector → **Upgrade**. Migration `19.0.1.8.0/post-migrate.py` runs: for every order line whose name
   differs from its product name, the name moves to `sf_combined_description` and the name becomes the product name.
   Check a few lines afterwards: the name is the product name; the description is in the new column.
2. Odoo: Settings → Companies → each company → **Invoice Layout** tab. Fill in (see the list below). Empty fields print nothing.
3. Odoo: print one invoice per company. Check the paper size (Letter for mTab LLC, A4 for G.A.M.M.A).
4. `git pull --ff-only`, then `python3 -m connector.check_connections`.
5. The connector now writes `sf_combined_description`, not the line name. `SF_HOLD_NAME_UPDATES` (true for now) holds
   changes to it. The run log says `description held (SF_HOLD_NAME_UPDATES) line ...`.

**Per-company settings to fill in** (Settings → Companies → Invoice Layout):
- mTab LLC: trading name (empty), tax ID, header block (empty), notes, payment and wire instructions, legal payment
  paragraph, footer (finance and support emails, website, phone, legal name and registration line).
- G.A.M.M.A: trading name "A trading name of GAMMA ASSOCIATES LTD.", header block (the Registered Office block with
  the VAT number), notes, payment and wire instructions, footer.
- Both: early payment offers (days and percent). Defaults: 10 days, 3%; 21 days, 2%.

**Test with the Salesforce admin**
- Information-only line: it is on the order and invoiced at 0, but it is not printed on the invoice or on the
  quotation. Its deliverables still appear in the Delivery Tracker.
- Description: an opportunity line shows the product name once on the invoice, with its Salesforce description after it
  in brackets, in italics.
- Description change in Salesforce: the Odoo line keeps its old description while `SF_HOLD_NAME_UPDATES` is true; the
  log shows it as held.
- Fulfilment (issue C) still open: see the note in the summary; Delivered quantity on the Salesforce line is not yet
  confirmed against the Delivery Tracker.

## Upgrade to sf_connector 19.0.1.7.0 (invoice PDFs)

Order: upgrade the Odoo module first, then pull the code (same reason as 19.0.1.6.0 above).

1. Odoo: Apps → sf_connector → **Upgrade**. New fields only (`sf_pdf_checksum`, `sf_pdf_sent_at` on invoices); no
   migration. The new method `sf_invoice_pdf` is called through JSON-2 by the connector.
2. The connector's Odoo API user needs read access to invoices, invoice lines and sale orders, the right to call
   `account.move.sf_invoice_pdf`, and write access to `sf_pdf_checksum` and `sf_pdf_sent_at` on invoices.
3. `git pull --ff-only`, then `python3 -m connector.check_connections`.
4. Turn it on in `connector/.env` only after a dry run: `SF_INVOICE_PDF=true` (and `SF_INVOICE_PDF_REFUNDS`, default
   true). It works with `SF_WRITEBACK=false`: its only Salesforce writes are Attachment records, never an Opportunity,
   an OpportunityLineItem or an `Odoo_*` field.
5. The log line `Invoice PDFs (...): attached a, replaced r, unchanged u, failed f` ends the step. `FAILED` lines name
   the invoice, the opportunity and the reason.

Where to look in Salesforce: the opportunity's **Notes & Attachments** related list shows `<invoice number>.pdf`.

## Checking the logs

```
# Last run's summary
tail -n 5 /opt/sfsync/logs/sync-$(date +%Y%m%d).log

# Errors and warnings from today
grep -E '^ERROR|^WARN|^Warnings' /opt/sfsync/logs/sync-$(date +%Y%m%d).log

# Skipped runs (a previous run still held the lock)
grep -i 'skip' /opt/sfsync/logs/sync-$(date +%Y%m%d).log

# Is a run in progress right now?
flock -n /opt/sfsync/sync.lock -c 'echo idle' || echo 'run in progress'

# Cron is installed
crontab -l
```

A run ends with a `Done (...)` line with counts for created, updated, unchanged and errors.
Grouped warnings appear before it, with examples. `ERROR` lines name the record that failed.
Each Odoo retry is printed as `retry n/3`.

To check both connections without changing anything, run from the repo directory:
`python3 -m connector.check_connections`.

## Salesforce write-back (phase 1)

After the Odoo side of each run, the connector writes Odoo ids, links, sync errors and line
quantities back to Salesforce. Rules: `docs/field-mapping.md` §5.1. It is off until `SF_WRITEBACK=true`.

**Before the first real write**

1. `python -m connector.check_connections`: Salesforce must log in, and the org line must show the org
   you expect with `sandbox: True`.
2. `python -m connector.sync_accounts --dry-run` and `python -m connector.sync_orders --dry-run`.
   Each prints `[dry-run] would update <Object> <Id> ...` lines and a
   `Salesforce write-back (dry run, ...)` summary. Nothing is written.
3. Look for `would stop` lines. Each one names a field the integration user cannot write. Fix that in
   Salesforce first. The real run stops that object and nothing else.
4. Check that the `Odoo_Record__c` links open the right record in Odoo. The opportunity link uses
   `/odoo/action-sale.action_orders/<id>`, which is not yet checked against this Odoo version.
5. Set `SF_WRITEBACK=true` and run both syncs once. A second run must report `updated 0`.

**Reading the log**

- `Salesforce write-back (...)`: counts per object: updated, unchanged, failed, no Odoo record.
- `Salesforce records NOT written (n), grouped by error code`: one line per record Salesforce did not write,
  with its Id and name. A `FIELD_INTEGRITY_EXCEPTION` group is explained once, with the opportunities named
  in the error. Those opportunities have totals that do not match their products in Salesforce: a data
  problem in this org, to fix there. The run exits with code 4 (see Exit codes below).
- `Salesforce write-back ... stopped`: a check failed (org mismatch, missing setting, field problem).
  The Odoo side of the run still completed.
- Records are sent with `allOrNone` off, so one bad record does not stop the others. When a call fails
  as a whole, or every record in it fails, the call is split in halves and sent again until the failing
  records are alone. Good records in the same call are written. Lines of one opportunity are never split
  across calls, so if an opportunity's lines fail, all of them are reported.

**Exit codes.** 0 = clean. 2 = the removal brake (`EXIT 2`, see below). 4 = Salesforce records were not
written (`EXIT 4`). Both at once: 6. Exit 4 is a data problem to fix in Salesforce, not a crash: the
rest of the run was written.

**Safety settings**

- `SF_WRITEBACK=false` (the default): no writes, dry runs still work.
- `SF_REQUIRE_SANDBOX` (default `true`): before any Odoo or Salesforce write, the run reads
  `Organization` and refuses unless `IsSandbox` is true. Set it to `false` only when the connector is
  pointed at production on purpose.
- `SF_HOLD_NAME_UPDATES` (default `false`): when `true`, a change to a line's description (name) from Salesforce is
  logged (`name held (SF_HOLD_NAME_UPDATES) line ...`) and counted (`descriptions held`), but NOT written to Odoo. Set
  to `true` while a Salesforce change to descriptions is being explained (2026-11-11: about 1,700 descriptions changed
  during the write-back window). Quantity, price and other changes are still written.
- Field names are matched without regard to case. The connector reads the org's own API names from describe once per
  run (for example `Odoo_Id__c`), and uses them in SOQL and in the write-back body.
- `SF_EXPECTED_ORG_ID` (optional): if set, a real run is refused unless the org Id matches. Set it for
  production. A sandbox refresh changes the sandbox's org Id, so for a sandbox either leave it empty
  or update it after the refresh.
- Every run logs the org Id, name and sandbox flag first. A dry run also prints
  `[dry-run] a real run would refuse: ...` when a real run would stop.

**Removal brake.** A sync run never removes many lines at once. Limits (docs/field-mapping.md §4.4):
- `SF_MAX_REMOVE_LINES` (default 5) and `SF_MAX_REMOVE_RATIO` (default 0.3): more than this for one
  opportunity skips that opportunity's removals. The log says `REMOVAL BRAKE <Id> <Name>: ...`, and the
  opportunity's `Odoo_Sync_Error__c` says `N products missing from Salesforce ... Finance to review.`
- `SF_MAX_REMOVE_TOTAL` (default 50): more than this in one run skips every removal in the run. The sync
  still completes, the write-back runs, and the run **exits with code 2**. The cron log then shows
  `EXIT 2: the run brake stopped removals`. Treat exit 2 as an alert.
- `--allow-mass-removal --opportunity <SF_ID>`: applies the removals the brakes skip, for one opportunity
  a person has checked. It is refused without `--opportunity`. Check the dry run for that opportunity
  first.

**Sandbox refresh.** A refresh gives the sandbox a new org Id, and removes any custom field that is not
in the deployed metadata. After a refresh, expect:
- A refused real run if `SF_EXPECTED_ORG_ID` still has the old Id.
- `Salesforce write-back for <Object> would stop` (or `stopped`) with `does not exist or is not visible`
  for the `Odoo_*` fields. The field check reports it; the object is skipped until the fields are
  redeployed. The Odoo side still runs.
- `ODOO_PUBLIC_URL`: required; the record links are built from it.
- `SF_MIN_VISIBLE_RATIO` (default 0.9): a run stops, with exit 1 and no change, when Salesforce returns
  no accounts, opportunities or lines, or fewer opportunities than this share of the Odoo orders.
  A stopped run logs `ERROR Salesforce returned ...`. If it stops on a sandbox that worked before,
  check the integration user's record access first.

## Known limits (interim)

- The integration user must be able to see and edit the records and the `Odoo_*` fields. Until it
  can, write-back stays stopped and the server keeps the CLI login.

- The sf CLI login expires if the refresh token is revoked or the user's access changes. The
  next run then fails with a Salesforce error. Re-import a fresh login as above.
- The workstation and the server share one login. Any logout or revocation on either side
  stops the sync.
- Logs are kept 14 days and only on the server. Odoo and Salesforce keep no copy of the
  connector's output.
- Odoo runs on the same host as the connector, so a host outage stops both.

## Upgrade to sf_connector 19.0.1.8.10 (PDFs without write-back, 5-minute cron)

1. `git pull --ff-only`, then `python3 -m connector.check_connections`.
2. Install the wrapper from the repo and check the copy matches (see *Installing the wrapper*).
3. Odoo: Apps → sf_connector → **Upgrade**. The render key changed, so each sent invoice is rendered again on its next
   run, and its attachment in Salesforce is replaced in place (one replacement per invoice, no duplicates).
4. `SF_INVOICE_PDF=true` now works with `SF_WRITEBACK=false`. Its Salesforce writes are Attachments only.
5. Cron: change `*/15` to `*/5` (see *Cron*).

## Upgrade to sf_connector 19.0.1.8.11 (Item Description mirrors the Salesforce description)

Order matters: upgrade the Odoo module **first**, then pull the code. The sync now reads `sf_study_notes`, which
exists only after the upgrade.

1. Odoo: Apps → sf_connector → **Upgrade**. The migration `19.0.1.8.11/post-migrate.py` runs once. It copies
   `sf_combined_description` to `item_description` on every synced line, including delivered and invoiced lines
   (display only). Expect about **4,531** lines changed on the sandbox (8 of them delivered or invoiced). The upgrade
   log does not print the count, so check it with: `search_count([('sf_line_id','!=',False)])` and compare
   `item_description` with `sf_combined_description` on those lines.
2. `git pull --ff-only`, then `python3 -m connector.check_connections`.
3. Refresh the wrapper if `deploy/run_sync.sh` changed (see *Installing the wrapper*).
4. The next run writes `sf_study_notes` on each line (not locked). `item_description` follows
   `sf_combined_description`: a description change is held when `SF_HOLD_NAME_UPDATES=true`, and both fields are
   held together. Deliverables get the same text through the mTab module (it copies `sf_combined_description`), not
   from this connector.

