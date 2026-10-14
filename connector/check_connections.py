"""Check that Odoo and Salesforce can be reached with the current settings. Read only.

    python -m connector.check_connections
"""
import sys

from . import config
from .odoo_client import OdooClient, OdooError
from .sf_client import SalesforceClient, SalesforceError, get_session

STAGE_FILTER = f"StageName = '{config.STAGE_AWARD_WON}'"


def check_odoo():
    odoo = OdooClient(config.ODOO_URL, config.ODOO_DB, config.ODOO_API_KEY)
    companies = odoo.search_read("res.company", [], ["id", "name"])
    print(f"  companies: {', '.join(c['name'] for c in companies) or '(none)'}")
    modules = odoo.search_read(
        "ir.module.module",
        [("name", "in", ["sf_connector", "lb_item_deliverables"])],
        ["id", "name", "state", "installed_version"],
    )
    for module in modules:
        print(f"  module {module['name']}: {module['state']} ({module['installed_version'] or '-'})")
    if not modules:
        print("  modules sf_connector and lb_item_deliverables not visible to this user")


def check_salesforce():
    session = get_session()
    sf = SalesforceClient(session)
    print(f"  instance {session.instance_url}, API v{sf.api_version}")
    rows = sf.query(f"SELECT COUNT(Id) total FROM Opportunity WHERE {STAGE_FILTER}")
    print(f"  opportunities at stage 07: {rows[0]['total']}")


def main():
    failed = False
    for name, check in (("Odoo", check_odoo), ("Salesforce", check_salesforce)):
        print(f"{name}:")
        try:
            check()
            print("  OK")
        except (OdooError, SalesforceError) as err:
            failed = True
            print(f"  FAILED: {err}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
