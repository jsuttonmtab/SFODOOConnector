"""Settings and mapping tables for the connector.

Connection settings come from connector/.env (or the real environment, which wins).
Mapping tables mirror docs/field-mapping.md.
"""
import os
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

# Odoo 19 JSON-2 API. The API key identifies the user, so no login is needed.
ODOO_URL = os.environ.get("ODOO_URL", "")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")

# Salesforce org alias for the sf CLI.
SF_TARGET_ORG = os.environ.get("SF_TARGET_ORG", "mtab-sandbox")

# §4.6: login of the Odoo user who gets the To Do when a synced opportunity leaves Award Won.
FINANCE_ACTIVITY_USER = os.environ.get("FINANCE_ACTIVITY_USER", "").strip()

# §4.3: only opportunities at this stage create orders (and so bring their account across).
STAGE_AWARD_WON = "07: Award Won / Contract signed"

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
