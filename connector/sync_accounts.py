"""Sync Salesforce Accounts that have a stage-07 opportunity into Odoo customers (res.partner).

Mapping: docs/field-mapping.md §1, §4.1, §4.1a, §4.3. Upsert key: res.partner.sf_account_id.
Parent accounts (ParentId chain) are synced too, as companies, so parent_id links resolve.
Salesforce is read only. Nothing is written back.

    python -m connector.sync_accounts --dry-run   # report what would change, write nothing
    python -m connector.sync_accounts             # create and update in Odoo
"""
import argparse
import html
import re
import sys
from collections import Counter, defaultdict

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

# Sandbox masks some user emails with a ".invalid" suffix.
INVALID_EMAIL_SUFFIX = re.compile(r"\.invalid$", re.IGNORECASE)

# Returned by parent_id() for a parent that only exists in this run's dry run.
PENDING = object()


def soql_literal(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def test_name_filter():
    names = ", ".join(soql_literal(n) for n in sorted(config.TEST_ACCOUNT_NAMES))
    return f"AND Name NOT IN ({names})" if names else ""


def build_soql():
    return f"""SELECT {', '.join(SF_FIELDS)}
FROM Account
WHERE celigo_sfnsio__Test_Mode_Record__c = false
{test_name_filter()}
AND Id IN (SELECT AccountId FROM Opportunity
  WHERE StageName = {soql_literal(config.STAGE_AWARD_WON)}
  AND celigo_sfnsio__Test_Mode_Record__c = false)"""


def build_parent_soql(ids):
    id_list = ", ".join(soql_literal(i) for i in ids)
    return f"""SELECT {', '.join(SF_FIELDS)}
FROM Account
WHERE Id IN ({id_list})
AND celigo_sfnsio__Test_Mode_Record__c = false
{test_name_filter()}"""


def fetch_parent_chain(sf, accounts):
    """Walk ParentId upwards from the qualifying accounts. Returns the parent accounts only."""
    visited = {acc["Id"] for acc in accounts}
    frontier = {acc["ParentId"] for acc in accounts if acc.get("ParentId")} - visited
    parents = []
    while frontier:
        visited |= frontier
        ids = sorted(frontier)
        found = []
        for i in range(0, len(ids), 200):
            found += sf.query(build_parent_soql(ids[i:i + 200]))
        parents += found
        frontier = {p["ParentId"] for p in found if p.get("ParentId")} - visited
    return parents


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
        self.warnings = defaultdict(list)  # kind -> ["<account>: <detail>", ...]
        self.pricelist_pending = set()     # currencies whose pricelist would be created (dry run)
        self.parent_ids = set()

    # ----- reference data -----

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

        self.currencies = {
            c["name"]: c for c in odoo.search_read(
                "res.currency", [("active", "in", [True, False])], ["id", "name", "active"])
        }
        currency_names = {c["id"]: c["name"] for c in self.currencies.values()}
        self.pricelists = {}
        for pricelist in odoo.search_read("product.pricelist", [], ["id", "name", "currency_id", "company_id"]):
            code = currency_names.get(m2o_id(pricelist["currency_id"]))
            if code:
                self.pricelists.setdefault(code, []).append(pricelist)
        # Shared pricelists first, then lowest id. Used only when a currency has several.
        for candidates in self.pricelists.values():
            candidates.sort(key=lambda p: (bool(m2o_id(p["company_id"])), p["id"]))

    def prepare_pricelists(self, accounts):
        """Make sure every currency used has a pricelist. Creates 'SF <CUR>' when missing.

        Raises before any write if a currency is missing or inactive, so the run stops early.
        """
        codes = {text(acc.get("CurrencyIsoCode")) for acc in accounts} - {False}
        for code in sorted(codes):
            if self.pricelists.get(code):
                continue
            currency = self.currencies.get(code)
            if not currency:
                raise OdooError(f"currency {code} does not exist in Odoo; cannot sync accounts in {code}")
            if not currency["active"]:
                raise OdooError(
                    f"currency {code} is inactive in Odoo; activate it, then rerun (no accounts were written)"
                )
            name = f"SF {code}"
            if self.dry_run:
                self.pricelist_pending.add(code)
                print(f"[dry-run] would create pricelist {name} (shared, {code})")
                continue
            new_id = self.odoo.create(
                "product.pricelist", [{"name": name, "currency_id": currency["id"], "company_id": False}]
            )[0]
            self.pricelists[code] = [{"id": new_id, "company_id": False}]
            print(f"created pricelist {name} (id {new_id}, shared, {code})")
            self.counts["pricelists_created"] += 1

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
        role = " (parent)" if acc["Id"] in self.parent_ids else ""
        print(f"{prefix}{action}: {acc['Id']} {acc['Name']}{role}" + (f" | {detail}" if detail else ""))

    def warn(self, kind, detail, acc=None):
        where = f"{acc['Id']} {acc['Name']}" if acc else ""
        entry = f"{where}: {detail}" if where and detail else (where or detail)
        self.warnings[kind].append(entry)

    def error(self, acc, err):
        self.counts["errors"] += 1
        print(f"ERROR {acc['Id']} {acc['Name']}: {err}")

    def print_warnings(self):
        if not self.warnings:
            print("\nNo warnings.")
            return
        total = sum(len(v) for v in self.warnings.values())
        print(f"\nWarnings ({total}), grouped by type:")
        for kind, examples in sorted(self.warnings.items(), key=lambda kv: -len(kv[1])):
            print(f"  {len(examples):>5}  {kind}")
            for example in examples[:3]:
                print(f"           e.g. {example}")

    # ----- field mapping -----

    def user_id(self, acc):
        email = text((acc.get("Account_Manager__r") or {}).get("Email"))
        if not email:
            return False
        email = INVALID_EMAIL_SUFFIX.sub("", email)
        user = self.users.get(email.lower())
        if not user:
            self.warn("no Odoo user for account manager (user left blank)", email, acc)
        return user or False

    def pricelist_id(self, acc):
        code = text(acc.get("CurrencyIsoCode"))
        if not code:
            return False
        if code in self.pricelist_pending:
            return f"new pricelist SF {code}"
        candidates = self.pricelists[code]
        if len(candidates) > 1:
            self.warn("multiple pricelists for a currency (lowest id used)",
                      f"{code}: {len(candidates)} candidates", acc)
        return candidates[0]["id"]

    def billing_entity_id(self, acc):
        raw = text(acc.get("mTab_Billing_Entity__c"))
        if not raw or raw in config.PLACEHOLDER_VALUES:
            return False
        company_name = config.BILLING_ENTITY_COMPANIES.get(raw)
        if not company_name:
            self.warn("unknown billing entity (left blank)", repr(raw), acc)
            return False
        company_id = self.companies.get(company_name)
        if not company_id:
            self.warn("Odoo company not found (left blank)", repr(company_name), acc)
            return False
        return company_id

    def address(self, acc, prefix):
        """Address fields for Billing or Shipping. Returns (vals, country_id)."""
        label = prefix.lower()
        country_raw = text(acc.get(f"{prefix}Country"))
        country_id = self.addr.country_id(country_raw)
        if country_raw and not country_id:
            self.warn(f"{label} country not matched (left blank)", repr(country_raw), acc)

        state_raw = text(acc.get(f"{prefix}State"))
        state_id = self.addr.state_id(country_id, state_raw) if state_raw else None
        if state_raw and not state_id:
            self.warn(f"{label} state not matched (left blank)", f"{state_raw!r} in {country_raw!r}", acc)

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
            self.warn("no billing country (left blank, 4.1a)", "", acc)
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

    def run(self, accounts, parents=()):
        all_accounts = list(accounts) + list(parents)
        self.parent_ids = {p["Id"] for p in parents}
        self.load_reference()
        self.prepare_pricelists(all_accounts)
        self.load_existing()

        batch = {acc["Id"] for acc in all_accounts}
        odoo_ids = {sf_id: rec["id"] for sf_id, rec in self.existing.items()}
        synced = set()

        # Pass 1: create or update each account's own fields.
        for acc in all_accounts:
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
        for acc in all_accounts:
            sf_id = acc["Id"]
            if sf_id not in synced or (odoo_ids.get(sf_id) is None and not self.dry_run):
                continue
            try:
                parent = self.parent_id(acc, odoo_ids, batch)
                if parent is None:
                    self.warn("parent not synced: test-mode, excluded, or not in Salesforce (link left blank)",
                              acc.get("ParentId", ""), acc)
                    continue
                if parent is PENDING:
                    self.report("link parent", acc, "parent is created in this run")
                    continue
                rec = self.existing.get(sf_id)
                current = m2o_id(rec["parent_id"]) if rec else False
                if parent == current:
                    continue
                self.report("set parent", acc, f"parent_id: {show(current)} -> {show(parent)}")
                if not self.dry_run:
                    self.odoo.write("res.partner", [odoo_ids[sf_id]], {"parent_id": parent})
                self.counts["parents_set"] += 1
            except OdooError as err:
                self.error(acc, err)

        # Pass 3: delivery address (child partner) when shipping differs from billing.
        for acc in all_accounts:
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
        self.print_warnings()
        c = self.counts
        mode = "dry run, nothing written" if self.dry_run else "written to Odoo"
        print(
            f"\nDone ({mode}): created {c['created']}, updated {c['updated']}, "
            f"unchanged {c['unchanged']}, parents set {c['parents_set']}, "
            f"delivery addresses created {c['delivery_created']} / updated {c['delivery_updated']}, "
            f"pricelists created {c['pricelists_created']}, errors {c['errors']}"
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
        parents = fetch_parent_chain(sf, accounts)
    except (OdooError, SalesforceError) as err:
        print(f"ERROR {err}")
        return 1

    print(f"{len(accounts)} accounts with a {config.STAGE_AWARD_WON!r} opportunity, "
          f"{len(parents)} parent accounts added")
    try:
        AccountSync(odoo, args.dry_run).run(accounts, parents)
    except OdooError as err:
        print(f"ERROR {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
