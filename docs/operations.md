# Operations: sandbox sync on mpl-odoo

> **Interim setup.** This runs the connector against the sandbox Odoo on mpl-odoo. It stays in place
> until two decisions are made:
> 1. **Cloud Odoo**: where production Odoo runs, and whether the connector moves with it.
> 2. **Salesforce integration user**: the connector logs in with the OAuth client-credentials flow
>    when `SF_CLIENT_ID` and `SF_CLIENT_SECRET` are set. Without them it falls back to the CLI login
>    below. The server still uses the CLI login until the integration user can see and update records
>    (see *Salesforce write-back*).

## What runs where

| Piece | Where |
|---|---|
| Odoo 19 sandbox (`OdooSandbox19`) | mpl-odoo, port 8069 |
| Connector code | `/opt/sfsync/SFODOOConnector` |
| Connector settings | `/opt/sfsync/SFODOOConnector/connector/.env` (git-ignored) |
| Scheduled run | cron, as the `sfsync` user, every 15 minutes |
| Wrapper script | `/opt/sfsync/run_sync.sh` |
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
SF_EXPECTED_ORG_ID=<18-character Id of the org the write-back may write to>
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

The cron job calls this wrapper. It:

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

## Cron

In the `sfsync` user's crontab (`crontab -e` as `sfsync`):

```
*/15 * * * * /opt/sfsync/run_sync.sh
```

Every 15 minutes. Because of the lock, a slow run delays the next one rather than overlapping
it.

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

1. `python -m connector.check_connections`: Salesforce must show the expected org and log in.
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
- `Salesforce failures (n), grouped by type`: one line per error kind, with an example Id.
- `Salesforce write-back ... stopped`: a check failed (org mismatch, missing setting, field problem).
  The Odoo side of the run still completed.
- Records are sent with `allOrNone` off, so one bad record does not stop the others.

**Safety settings**

- `SF_WRITEBACK=false` (the default): no writes, dry runs still work.
- `SF_EXPECTED_ORG_ID`: a real write is refused unless the login's org Id matches.
- `ODOO_PUBLIC_URL`: required; the record links are built from it.

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
