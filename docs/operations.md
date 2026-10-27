# Operations: sandbox sync on mpl-odoo

> **Interim setup.** This runs the connector against the sandbox Odoo on mpl-odoo and uses a
> personal Salesforce CLI login. It stays in place until two decisions are made:
> 1. **Cloud Odoo**: where production Odoo runs, and whether the connector moves with it.
> 2. **Salesforce integration user**: a dedicated user with an OAuth client-credentials app
>    replaces the CLI login. Only `connector/sf_client.get_session()` changes.

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
SF_TARGET_ORG=mtab-sandbox
FINANCE_ACTIVITY_USER=<Odoo login of the Finance user>
DELIVERABLES_PROJECT=DS deliverables
ODOO_API_LOGIN=<Odoo login the API key belongs to; optional, used only for a warning>
AUTO_CREATE_PRODUCTS=false
```

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

## Known limits (interim)

- The sf CLI login expires if the refresh token is revoked or the user's access changes. The
  next run then fails with a Salesforce error. Re-import a fresh login as above.
- The workstation and the server share one login. Any logout or revocation on either side
  stops the sync.
- Logs are kept 14 days and only on the server. Odoo and Salesforce keep no copy of the
  connector's output.
- Odoo runs on the same host as the connector, so a host outage stops both.
