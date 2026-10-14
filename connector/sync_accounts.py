"""Sync Salesforce Accounts that have a stage-07 opportunity into Odoo customers (res.partner).

Mapping: docs/field-mapping.md §1, §4.1, §4.1a, §4.3. Upsert key: res.partner.sf_account_id.
Salesforce is read only. Nothing is written back.

    python -m connector.sync_accounts --dry-run   # report what would change, write nothing
    python -m connector.sync_accounts             # create and update in Odoo
"""
import argparse
import html
import sys
from collections import Counter

from . import config
from .countries import AddressMatcher
from .odoo_client import OdooClient, OdooError, m2o_id
from .sf_client import SalesforceClient, SalesforceError, get_session

SF_FIELDS = [
    "Id", "Name", "ParentId", "Legal_Entity_Name__c", "Phone", "Website", "Description",
    "VAT_Number_Text__c", "mTab_Billing_Entity__c", "Account_Manager__r.Email", "CurrencyIsoCode",
    "Billing_Distribution_Method__c",
    "BillingStreet", "BillingCity", "BillingState", "BillingPostalCode", "BillingCountry",
    "ShippingStreet", "ShippingCity", "ShippingState", "ShippingPostalCode", "ShippingCountry",
]

# res.partner fields compared on every run.
PARTNER_FIELDS = [
    "id", "sf_account_id", "name", "sf_legal_entity_name", "phone", "website", "comment", "vat",
    "sf_billing_distribution_method", "user_id", "property_product_pricelist",
    "sf_default_billing_entity_id", "parent_id", "street", "city", "zip", "state_id", "country_id",
]
M2O_FIELDS = {
    "user_id", "property_product_pricelist", "sf_default_billing_entity_id",
    "parent_id", "state_id", "country_id",
}
CHILD_FIELDS = ["id", "parent_id", "name", "street", "city", "zip", "state_id", "country_id"]

# Returned by parent_id() for a parent that only exists in this run's dry run.
PENDING = object()


def soql_literal(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_soql():
    names = ", ".join(soql_literal(n) for n in sorted(config.TEST_ACCOUNT_NAMES))
    name_filter = f"AND Name NOT IN ({names})" if names else ""
    return f"""SELECT {', '.join(SF_FIELDS)}
FROM Account
WHERE celigo_sfnsio__Test_Mode_Record__c = false
{name_filter}
AND Id IN (SELECT AccountId FROM Opportunity
  WHERE StageName = {soql_literal(config.STAGE_AWARD_WON)}
  AND celigo_sfnsio__Test_Mode_Record__c = false)"""


def text(value):
    """Stripped string, or False for blank (Odoo's empty value)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return False


def comment_html(value):
    """§1: Description goes to the html field `comment`. Plain text is escaped."""
    value = text(value)
    if not value:
        return False
    return "<br/>".join(html.escape(line) for line in value.splitlines())


def diff(record, vals):
    """List of (field, current, wanted) for fields whose values differ."""
    changes = []
    for field, wanted in vals.items():
        if field in M2O_FIELDS:
            current = m2o_id(record.get(field))
        else:
            current = record.get(field) or False
        if current != wanted:
            changes.append((field, current, wanted))
    return changes


def show(value):
    if value is False or value is None or value == "":
        return "-"
    return str(value)[:60]


def address_key(acc, prefix):
    return tuple(str(acc.get(f"{prefix}{f}") or "").strip().lower()
                 for f in ("Street", "City", "State", "PostalCode", "Country"))


class AccountSync:
    def __init__(self, odoo, dry_run):
        self.odoo = odoo
        self.dry_run = dry_run
        self.counts = Counter()
        self.warned = set()

    # ----- reference data (read once per run) -----

    def load_reference(self):
        odoo = self.odoo
        self.addr = AddressMatcher(
            odoo.search_read("res.country", [], ["id", "name", "code"]),
            odoo.search_read("res.country.state", [], ["id", "name", "code", "country_id"]),
        )

        self.users = {}
        for user in odoo.search_read("res.users", [], ["id", "login", "email"]):
            for key in (user["login"], user["email"]):
                if key:
                    self.users.setdefault(key.lower(), user["id"])

        self.companies = {c["name"]: c["id"] for c in odoo.search_read("res.company", [], ["id", "name"])}

        currency_names = {c["id"]: c["name"] for c in odoo.search_read("res.currency", [], ["id", "name"])}
        self.pricelists = {}
        for pricelist in odoo.search_read("product.pricelist", [], ["id", "name", "currency_id", "company_id"]):
            code = currency_names.get(m2o_id(pricelist["currency_id"]))
            if code:
                self.pricelists.setdefault(code, []).append(pricelist)
        # Shared pricelists first, then lowest id. Used only when a currency has several.
        for candidates in self.pricelists.values():
            candidates.sort(key=lambda p: (bool(m2o_id(p["company_id"])), p["id"]))

    def load_existing(self):
        """Partners that already carry an sf_account_id, and their delivery children."""
        rows = self.odoo.search_read(
            "res.partner",
            [("sf_account_id", "!=", False), ("active", "in", [True, False])],
            PARTNER_FIELDS,
        )
        self.existing = {row["sf_account_id"]: row for row in rows}

        self.children = {}
        parent_ids = [row["id"] for row in rows]
        if parent_ids:
            children = self.odoo.search_read(
                "res.partner",
                [("type", "=", "delivery"), ("parent_id", "in", parent_ids)],
                CHILD_FIELDS,
            )
            for child in children:
                self.children.setdefault(m2o_id(child["parent_id"]), child)

    # ----- output -----

    def report(self, action, acc, detail=""):
        prefix = "[dry-run] would " if self.dry_run else ""
        print(f"{prefix}{action}: {acc['Id']} {acc['Name']}" + (f" | {detail}" if detail else ""))

    def warn(self, message, acc=None):
        self.counts["warnings"] += 1
        where = f"{acc['Id']} {acc['Name']}: " if acc else ""
        print(f"WARN {where}{message}")

    def warn_once(self, key, message):
        if key not in self.warned:
            self.warned.add(key)
            self.warn(message)

    def error(self, acc, err):
        self.counts["errors"] += 1
        print(f"ERROR {acc['Id']} {acc['Name']}: {err}")

    # ----- field mapping -----

    def user_id(self, acc):
        email = text((acc.get("Account_Manager__r") or {}).get("Email"))
        if not email:
            return False
        user = self.users.get(email.lower())
        if not user:
            self.warn(f"no Odoo user for account manager {email}", acc)
        return user or False

    def pricelist_id(self, acc):
        code = text(acc.get("CurrencyIsoCode"))
        if not code:
            return False
        candidates = self.pricelists.get(code, [])
        if not candidates:
            self.warn_once(f"pricelist:{code}", f"no Odoo pricelist for currency {code}")
            return False
        if len(candidates) > 1:
            self.warn_once(
                f"pricelists:{code}",
                f"{len(candidates)} pricelists for {code}; using id {candidates[0]['id']}",
            )
        return candidates[0]["id"]

    def billing_entity_id(self, acc):
        raw = text(acc.get("mTab_Billing_Entity__c"))
        if not raw or raw in config.PLACEHOLDER_VALUES:
            return False
        company_name = config.BILLING_ENTITY_COMPANIES.get(raw)
        if not company_name:
            self.warn(f"unknown billing entity {raw!r}", acc)
            return False
        company_id = self.companies.get(company_name)
        if not company_id:
            self.warn(f"Odoo company {company_name!r} not found", acc)
            return False
        return company_id

    def address(self, acc, prefix):
        """Address fields for Billing or Shipping. Returns (vals, country_id)."""
        country_raw = text(acc.get(f"{prefix}Country"))
        country_id = self.addr.country_id(country_raw)
        if country_raw and not country_id:
            self.warn(f"{prefix.lower()} country {country_raw!r} not matched (4.1a)", acc)

        state_raw = text(acc.get(f"{prefix}State"))
        state_id = self.addr.state_id(country_id, state_raw) if state_raw else None
        if state_raw and not state_id:
            self.warn(f"{prefix.lower()} state {state_raw!r} not matched", acc)

        return {
            "street": text(acc.get(f"{prefix}Street")),
            "city": text(acc.get(f"{prefix}City")),
            "zip": text(acc.get(f"{prefix}PostalCode")),
            "state_id": state_id or False,
            "country_id": country_id or False,
        }, country_id

    def billing_vals(self, acc):
        vals = {
            "name": acc["Name"],
            "sf_legal_entity_name": text(acc.get("Legal_Entity_Name__c")),
            "phone": text(acc.get("Phone")),
            "website": text(acc.get("Website")),
            "comment": comment_html(acc.get("Description")),
            "vat": text(acc.get("VAT_Number_Text__c")),
            "sf_billing_distribution_method": text(acc.get("Billing_Distribution_Method__c")),
            "user_id": self.user_id(acc),
            "property_product_pricelist": self.pricelist_id(acc),
            "sf_default_billing_entity_id": self.billing_entity_id(acc),
        }
        address, country_id = self.address(acc, "Billing")
        vals.update(address)
        if not country_id:
            self.warn("synced without a billing country (4.1a)", acc)
        return vals

    def parent_id(self, acc, odoo_ids, batch):
        parent_sf = acc.get("ParentId")
        if not parent_sf:
            return False
        if odoo_ids.get(parent_sf):
            return odoo_ids[parent_sf]
        if self.dry_run and parent_sf in batch:
            return PENDING
        return None

    def delivery_needed(self, acc):
        shipping = address_key(acc, "Shipping")
        return any(shipping) and shipping != address_key(acc, "Billing")

    # ----- run -----

    def run(self, accounts):
        self.load_reference()
        self.load_existing()
        batch = {acc["Id"] for acc in accounts}
        odoo_ids = {sf_id: rec["id"] for sf_id, rec in self.existing.items()}
        synced = set()

        # Pass 1: create or update each account's own fields.
        for acc in accounts:
            sf_id = acc["Id"]
            try:
                vals = self.billing_vals(acc)
                rec = self.existing.get(sf_id)
                if rec:
                    changes = diff(rec, vals)
                    if not changes:
                        self.counts["unchanged"] += 1
                        synced.add(sf_id)
                        continue
                    detail = ", ".join(f"{f}: {show(c)} -> {show(w)}" for f, c, w in changes)
                    self.report("update", acc, detail)
                    if not self.dry_run:
                        self.odoo.write("res.partner", [rec["id"]], {f: w for f, _, w in changes})
                    self.counts["updated"] += 1
                else:
                    country = "country set" if vals["country_id"] else "no country"
                    self.report("create", acc, f"{show(vals['city'])}, {country}")
                    if not self.dry_run:
                        new_id = self.odoo.create(
                            "res.partner", [{**vals, "is_company": True, "sf_account_id": sf_id}]
                        )[0]
                        odoo_ids[sf_id] = new_id
                    self.counts["created"] += 1
                synced.add(sf_id)
            except OdooError as err:
                self.error(acc, err)

        # Pass 2: link parents now that every account in this batch has an id.
        for acc in accounts:
            sf_id = acc["Id"]
            if sf_id not in synced or (odoo_ids.get(sf_id) is None and not self.dry_run):
                continue
            try:
                parent = self.parent_id(acc, odoo_ids, batch)
                if parent is None:
                    self.warn("parent account is not in Odoo; parent link left blank", acc)
                    continue
                rec = self.existing.get(sf_id)
                current = m2o_id(rec["parent_id"]) if rec else False
                if parent is PENDING:
                    self.report("link parent", acc, "parent is created in this run")
                    continue
                if parent == current:
                    continue
                self.report("set parent", acc, f"parent_id: {show(current)} -> {show(parent)}")
                if not self.dry_run:
                    self.odoo.write("res.partner", [odoo_ids[sf_id]], {"parent_id": parent})
                self.counts["parents_set"] += 1
            except OdooError as err:
                self.error(acc, err)

        # Pass 3: delivery address (child partner) when shipping differs from billing.
        for acc in accounts:
            sf_id = acc["Id"]
            if sf_id not in synced or (odoo_ids.get(sf_id) is None and not self.dry_run):
                continue
            if not self.delivery_needed(acc):
                continue
            try:
                self.sync_delivery(acc, odoo_ids.get(sf_id))
            except OdooError as err:
                self.error(acc, err)

        self.summary()

    def sync_delivery(self, acc, partner_id):
        vals, _ = self.address(acc, "Shipping")
        vals["name"] = acc["Name"]
        child = self.children.get(partner_id) if partner_id else None
        if child:
            changes = diff(child, vals)
            if not changes:
                self.counts["delivery_unchanged"] += 1
                return
            detail = ", ".join(f"{f}: {show(c)} -> {show(w)}" for f, c, w in changes)
            self.report("update delivery address", acc, detail)
            if not self.dry_run:
                self.odoo.write("res.partner", [child["id"]], {f: w for f, _, w in changes})
            self.counts["delivery_updated"] += 1
        else:
            self.report("create delivery address", acc)
            if not self.dry_run:
                vals.update({"parent_id": partner_id, "type": "delivery", "is_company": False})
                self.odoo.create("res.partner", [vals])
            self.counts["delivery_created"] += 1

    def summary(self):
        c = self.counts
        mode = "dry run, nothing written" if self.dry_run else "written to Odoo"
        print(
            f"\nDone ({mode}): created {c['created']}, updated {c['updated']}, "
            f"unchanged {c['unchanged']}, parents set {c['parents_set']}, "
            f"delivery addresses created {c['delivery_created']} / updated {c['delivery_updated']}, "
            f"warnings {c['warnings']}, errors {c['errors']}"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sync Salesforce Accounts to Odoo customers.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change; write nothing to Odoo")
    args = parser.parse_args(argv)

    try:
        odoo = OdooClient(config.ODOO_URL, config.ODOO_DB, config.ODOO_API_KEY)
        sf = SalesforceClient(get_session())
        accounts = sf.query(build_soql())
    except (OdooError, SalesforceError) as err:
        print(f"ERROR {err}")
        return 1

    print(f"{len(accounts)} accounts with a {config.STAGE_AWARD_WON!r} opportunity")
    try:
        AccountSync(odoo, args.dry_run).run(accounts)
    except OdooError as err:
        print(f"ERROR {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
