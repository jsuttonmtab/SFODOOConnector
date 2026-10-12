"""
Export Odoo field definitions for the Salesforce <-> Odoo connector design.

Run from any machine that can reach the Odoo server:
    python export_odoo_fields.py

Produces odoo_fields.json. Read-only: it only calls fields_get / search_read.
Use an Odoo user with read access to Sales, Accounting and Settings > Technical.
An API key (Preferences > Account Security > New API Key) can be used in place
of the password.
"""

import getpass
import json
import xmlrpc.client

URL = "http://10.10.100.25:8069"   # mpl-odoo; change if behind a proxy / https
DB = "OdooSandbox19"

# Core models the connector touches. Missing models are skipped, not fatal.
MODELS = [
    "res.partner",          # SFDC Account -> customer
    "res.company",          # mTab billing entity / subsidiary
    "res.currency",
    "res.country",
    "res.country.state",
    "sale.order",           # SFDC Opportunity -> sales order
    "sale.order.line",      # Opportunity line items
    "product.product",
    "product.template",
    "product.pricelist",    # Pricebook2Id
    "account.payment.term", # Payment_Terms__c
    "account.move",         # invoices (ERP -> SFDC)
    "account.move.line",
    "account.tax",
]

# Also pick up any custom models (e.g. the lb_item_deliverables module).
CUSTOM_MODEL_PREFIXES = ("lb.", "x_")

ATTRS = ["string", "type", "required", "readonly", "relation",
         "selection", "store", "help", "company_dependent"]


def main():
    user = input("Odoo login: ").strip()
    secret = getpass.getpass("Password or API key: ")

    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, user, secret, {})
    if not uid:
        raise SystemExit("Authentication failed.")
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)

    def call(model, method, *args, **kw):
        return models.execute_kw(DB, uid, secret, model, method, list(args), kw)

    # Discover custom models
    model_list = list(MODELS)
    for prefix in CUSTOM_MODEL_PREFIXES:
        found = call("ir.model", "search_read",
                     [["model", "=like", f"{prefix}%"]], fields=["model"])
        model_list += [m["model"] for m in found if m["model"] not in model_list]

    out = {"server": URL, "db": DB, "version": common.version(), "models": {}}

    for model in model_list:
        try:
            fields = call(model, "fields_get", attributes=ATTRS)
        except xmlrpc.client.Fault as e:
            print(f"  skip {model}: {e.faultString.splitlines()[-1]}")
            continue

        # Which module defined each field (shows custom vs. stock fields)
        meta = call("ir.model.fields", "search_read",
                    [["model", "=", model]], fields=["name", "modules", "state"])
        origin = {m["name"]: {"modules": m["modules"], "state": m["state"]}
                  for m in meta}
        for name, f in fields.items():
            f.update(origin.get(name, {}))

        out["models"][model] = fields
        print(f"  {model}: {len(fields)} fields")

    with open("odoo_fields.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False, default=str)
    print("\nWrote odoo_fields.json - attach it in the chat.")


if __name__ == "__main__":
    main()
