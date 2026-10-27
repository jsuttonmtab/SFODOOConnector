# Operations: sandbox sync on mpl-odoo

> **Interim setup.** This runs the connector against the sandbox Odoo on mpl-odoo and uses a
> personal Salesforce CLI login. It stays in place until two decisions are made:
> 1. **Cloud Odoo**: where production Odoo runs, and whether the connector moves with it.
> 2. **Salesforce integration user**: a dedicated user with an OAuth client-credentials app
>    replaces the CLI login. Only `connector/sf_client.get_session()` changes.
>
> Items marked **(confirm)** are details to verify on the server before relying on them.

## What runs where

| Piece | Where |
|---|---|
| Odoo 19 sandbox (`OdooSandbox19`) | mpl-odoo, port 8069 |
| Connector code (this repo) | `/opt/sfsync/SFODOOConnector` **(confirm path)** |
| Connector settings | `connector/.env` in the repo checkout (git-ignored) |
| Scheduled run | cron, as the `sfsync` user, every 15 minutes |
| Wrapper script | `/opt/sfsync/run_sync.sh` |
| Logs | `/opt/sfsync/logs/` **(confirm path)** |
| Salesforce login | `sf` CLI login for alias `mtab-sandbox`, owned by `sfsync` |

## The sfsync account

- A dedicated Linux user, `sfsync`, runs everything. It does not log in interactively and
  should not have sudo.
- Home directory: `/opt/sfsync`.
- The sf CLI login and the repo checkout both live under this home directory, so they are
  owned by `sfsync`.

## GitHub deploy key (read-only)

The server pulls the repo over SSH with a deploy key, not a personal key.

1. As `sfsync`, generate a key with no passphrase: `ssh-keygen -t ed25519 -f ~/.ssh/sfodoo_deploy -C "sfsync mpl-odoo"`.
2. In GitHub: repository **Settings → Deploy keys → Add deploy key**. Paste the `.pub` file.
   Leave **Allow write access** unticked. The key must stay read-only.
3. In `~/.ssh/config`, point GitHub at the key:

   ```
   Host github.com
       IdentityFile ~/.ssh/sfodoo_deploy
       IdentitiesOnly yes
   ```

4. Clone once: `git clone git@github.com:<org>/SFODOOConnector.git /opt/sfsync/SFODOOConnector` **(confirm org and path)**.

## connector/.env

`connector/.env` is git-ignored and must be created on the server. Start from
`connector/.env.example`. On mpl-odoo the connector talks to Odoo on the same machine, so the
URL is `localhost`:

```
ODOO_URL=http://localhost:8069
ODOO_DB=OdooSandbox19
ODOO_API_KEY=<API key of the integration user in Odoo>
SF_TARGET_ORG=mtab-sandbox
FINANCE_ACTIVITY_USER=<Odoo login of the Finance user>
AUTO_CREATE_PRODUCTS=false
```

- Create the API key in Odoo under Preferences → Account Security → New API Key, for the
  integration user. Don't use a personal user's key.
- Keep the file readable by `sfsync` only: `chmod 600 connector/.env`.
- Set `AUTO_CREATE_PRODUCTS` deliberately. `true` creates new service products in Odoo for
  Salesforce products with no match.

## Salesforce CLI login (import from an sfdx auth URL)

The login is imported, not created on the server, because the server has no browser.

1. On a machine with a working login for `mtab-sandbox`, print the auth URL:
   `sf org display --target-org mtab-sandbox --verbose --json` and copy `sfdxAuthUrl`.
   The field only appears for logins made with `sf org login web`. A JWT login has none.
   **Treat it as a password**: it contains a refresh token.
2. Save it to a file on the server readable only by `sfsync`, then import it as `sfsync`:

   ```
   umask 077
   printf '%s' '<sfdxAuthUrl>' > /opt/sfsync/auth.url
   sf org login sfdx-url --sfdx-url-file /opt/sfsync/auth.url --alias mtab-sandbox
   shred -u /opt/sfsync/auth.url
   ```

3. Check: `sf org display --target-org mtab-sandbox --json` shows the instance URL and the
   sandbox username.

Anyone who has the auth URL can use this login. Don't paste it into chat, tickets or commits.
The connector only reads from Salesforce, but the login has the same rights as the user who
created it.

## /opt/sfsync/run_sync.sh

The cron job calls this wrapper. What it must do:

- **Lock**: take `flock -n` on a lock file under `/opt/sfsync`. If the previous run still
  holds it, exit without running, so runs never overlap.
- **Run**, from the repo directory, the account sync and then the order sync. Order lines are
  part of the order sync. Use `--dry-run` only when checking, as a manual step.

  ```
  python -m connector.sync_accounts
  python -m connector.sync_orders
  ```

- **Log**: append to one log file per day, named by date, under `/opt/sfsync/logs/`.
  Capture stdout and stderr.
- **Retain 14 days**: delete logs older than 14 days, for example with
  `find /opt/sfsync/logs -name 'sync-*.log' -mtime +14 -delete`.

**(confirm)** The script on the server is the reference. If it differs from the above, update
this section.

## Cron

As `sfsync` (`crontab -e`):

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
- If the change includes the Odoo module (`odoo_addons/sf_connector`), upgrade the module in
  Odoo as well: Apps → sf_connector → Upgrade. Code pulled without the upgrade can fail when
  it writes to fields the database doesn't have yet.

## Checking the logs

```
# Last run's summary
tail -n 5 /opt/sfsync/logs/sync-$(date +%F).log

# Errors and warnings from today
grep -E '^ERROR|^WARN|^Warnings' /opt/sfsync/logs/sync-$(date +%F).log

# Is a run in progress?
flock -n /opt/sfsync/run_sync.lock -c 'echo idle' || echo 'run in progress'

# Cron is installed and running
crontab -l
```

A run ends with a `Done (...)` line with counts for created, updated, unchanged and errors.
Grouped warnings appear before it, with examples. `ERROR` lines name the record that failed.
Each Odoo retry is printed as `retry n/3`.

Also check the connection: `python -m connector.check_connections` from the repo directory.
It checks Odoo and Salesforce and changes nothing.

## Known limits (interim)

- The sf CLI login expires if the refresh token is revoked or the user's access changes. The
  next run then fails with a Salesforce error. Re-import the login as above.
- The 14-day retention applies to logs only. Odoo and Salesforce keep no copy of the connector's
  output.
- Odoo runs on the same host as the connector, so a host outage stops both.
