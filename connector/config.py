"""Settings and mapping tables for the connector.

Connection settings come from connector/.env (or the real environment, which wins).
Mapping tables mirror docs/field-mapping.md.
"""
import os
import sys
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env(path=ENV_FILE):
    """Read KEY=VALUE lines from .env into os.environ. Existing variables are kept."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


def use_utf8_output():
    """Print as UTF-8, and replace what cannot be encoded, so redirected output (a log file, the cron
    log) cannot crash on a character such as an arrow. Windows defaults redirected output to cp1252."""
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")


# Odoo 19 JSON-2 API. The API key identifies the user, so no login is needed.
ODOO_URL = os.environ.get("ODOO_URL", "")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")
# Optional: login of the user the API key belongs to. Used only to warn when that user is the
# FINANCE_ACTIVITY_USER (Odoo does not email an assignment to yourself).
ODOO_API_LOGIN = os.environ.get("ODOO_API_LOGIN", "").strip()

# Salesforce org alias for the sf CLI (used only when SF_CLIENT_ID/SECRET are not set).
SF_TARGET_ORG = os.environ.get("SF_TARGET_ORG", "mtab-sandbox")

# Salesforce OAuth client-credentials login (connector/sf_client.py). Secrets: never print or log.
SF_LOGIN_URL = os.environ.get("SF_LOGIN_URL", "").strip().rstrip("/")
SF_CLIENT_ID = os.environ.get("SF_CLIENT_ID", "").strip()
SF_CLIENT_SECRET = os.environ.get("SF_CLIENT_SECRET", "").strip()

# Odoo -> Salesforce write-back (connector/sf_writeback.py). Nothing is written to Salesforce unless true.
SF_WRITEBACK = os.environ.get("SF_WRITEBACK", "false").strip().lower() in ("1", "true", "yes")
# Writes are refused unless the authenticated org Id (18 characters) is this one.
SF_EXPECTED_ORG_ID = os.environ.get("SF_EXPECTED_ORG_ID", "").strip()
# How users reach Odoo, for the Odoo_Record__c links written to Salesforce.
ODOO_PUBLIC_URL = os.environ.get("ODOO_PUBLIC_URL", "").strip().rstrip("/")

# Safety guard (sf_client.require_visible): stop the run if Salesforce returns fewer than this share
# of the opportunities Odoo expects.
SF_MIN_VISIBLE_RATIO = float(os.environ.get("SF_MIN_VISIBLE_RATIO", "0.9"))

# Odoo_Sync_Error__c (sf_writeback.py): only the messages whose text contains one of these is written
# to Salesforce. Everything else stays in the sync log. Matched as plain substrings, case-sensitive.
# Add a phrase here to show a message to Sales; remove it to keep one internal.
SF_VISIBLE_ISSUES = (
    "VAT rejected",                  # account VAT Odoo would not accept (kept in sf_vat_rejected)
    "VAT not written",               # same, on later runs
    "unknown billing entity",        # billing entity picklist value with no Odoo company
    "billing country not matched",   # account billing country
    "billing state not matched",     # account billing state (and opportunity billing state)
    "no billing country",            # account has no billing country
    "opportunity billing country",   # opportunity billing country missing or not matched
    "not created:",                  # order not created: the reasons are listed after it
    "payment terms not set",         # opportunity has no payment terms
    "billing entity not set",        # opportunity has no billing entity
    "billing entity",                # billing entity not resolved to an Odoo company
    "no currency",                   # opportunity has no currency
    "locked field",                  # header field change rejected (§4.4)
    "a delivered line was removed",  # removed line is delivered: order not changed (§4.4)
    "locked (",                      # line field change rejected (§4.4)
    "no Odoo product",               # line product not in Odoo
    "has no description",            # line has no description
    "has no product",                # line has no product
    "line not created",              # line not created
    "stage moved out of",            # opportunity left Award Won: order not changed (§4.6)
)

# §4.6: login of the Odoo user who gets the To Do when a synced opportunity leaves Award Won.
FINANCE_ACTIVITY_USER = os.environ.get("FINANCE_ACTIVITY_USER", "").strip()

# Project set on the orders the connector manages, so the Delivery Tracker lists their deliverables.
# Set only when the order has no project yet. Must be an active, billable project.
DELIVERABLES_PROJECT = os.environ.get("DELIVERABLES_PROJECT", "DS deliverables").strip()

# §3: create a service product for a Salesforce product with no Odoo match (default off).
AUTO_CREATE_PRODUCTS = os.environ.get("AUTO_CREATE_PRODUCTS", "false").strip().lower() in ("1", "true", "yes")

# §4.3: only opportunities at this stage create orders (and so bring their account across).
STAGE_AWARD_WON = "07: Award Won / Contract signed"
STAGE_CLOSED = "Closed"

# §4.3: accounts on this list are never synced. Configurable.
TEST_ACCOUNT_NAMES = {"TestAccount", "Celigo Company"}

# §1 / §2: Salesforce billing entity picklist value -> Odoo res.company name.
BILLING_ENTITY_COMPANIES = {
    "mTab LLC": "mTab LLC",
    "G.A.M.M.A": "G.A.M.M.A",
}

# Picklist placeholder meaning "not set" (§2).
PLACEHOLDER_VALUES = {"This will prepopulate on save"}

# §4.1a alias table. Keys are lower case. Matched after name and ISO code.
COUNTRY_ALIASES = {
    "usa": "US",
    "us": "US",
    "united states": "US",
    "uk": "GB",
    "united kingdom": "GB",
    "uae": "AE",
    "u.a.e.": "AE",
    "united arab emirates": "AE",
    "korea": "KR",
    "south korea": "KR",
    "republic of korea": "KR",
    "china": "CN",
    "peoples republic of china": "CN",
    "prc (china)": "CN",
    "the netherlands": "NL",
    "russia": "RU",
    "turkey": "TR",
    "czech republic": "CZ",
    "vietnam": "VN",
    "taiwan": "TW",
}
