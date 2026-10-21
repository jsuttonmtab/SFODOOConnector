"""Sync Salesforce OpportunityLineItems to sale.order.line, for orders synced by sync_orders.

Mapping: docs/field-mapping.md §3 and §4.4. Upsert key: sale.order.line.sf_line_id.

Removed lines (§4.4): an Odoo line whose SF line is gone gets product_uom_qty = 0 and
sf_removed_in_sf = True. If it was delivered in any quantity, the whole order fails and
sync_orders changes nothing on it (see LineSync.blocked).

Used by sync_orders.OrderSync, which passes itself in as `parent` (for counts, warnings and errors).
"""
from . import config
from .odoo_client import m2o_id
from .sync_accounts import diff, show, soql_literal, text

LINE_SF_FIELDS = [
    "Id", "OpportunityId", "Product2Id", "Product2.ProductCode", "Product2.Name",
    "Combined_Product_Description__c", "Quantity", "Total_Billable_Amount__c", "Information_Only__c",
    "Year_of_Study__c", "Country_Region__c", "Version__c", "Study_Notes_Additional_Description__c",
    "Data_Vendor_netnew__c", "Coverage_Period__c", "StudyType__c", "Study_Name_Formula__c",
    "Wave_Period__c", "Up_Sell__c", "On_Hold__c", "Delivery__c", "Term_Start_Date__c",
    "Term_End_Date__c", "Billing_Schedule__c",
]

LINE_FIELDS = [
    "id", "order_id", "sf_line_id", "product_id", "name", "product_uom_qty", "price_unit", "discount",
    "qty_delivered", "qty_invoiced", "deliverable_delivered_count", "sf_removed_in_sf",
    "sf_billing_schedule", "year_of_study", "country_region", "version", "item_description",
    "sf_data_vendor", "sf_coverage_period", "sf_study_type", "sf_study_name", "sf_wave_period",
    "sf_delivery", "sf_is_upsell", "sf_information_only", "sf_on_hold", "sf_term_start_date",
    "sf_term_end_date",
]

LINE_M2O = {"product_id"}
DELIVERY_MAP = {"Classic": "classic", "Halo": "halo", "Both": "both"}
# §3: the billing schedule default is a placeholder, treated as empty.
BILLING_PLACEHOLDERS = {"Prepopulated on Save", "This will prepopulate on save"}


def fetch_sf_lines(sf, opp_ids):
    """OpportunityLineItems for the given opportunities, grouped by OpportunityId."""
    by_opp = {}
    ids = sorted(opp_ids)
    for i in range(0, len(ids), 150):
        id_list = ", ".join(soql_literal(x) for x in ids[i:i + 150])
        soql = (f"SELECT {', '.join(LINE_SF_FIELDS)} FROM OpportunityLineItem "
                f"WHERE OpportunityId IN ({id_list})")
        for row in sf.query(soql):
            by_opp.setdefault(row["OpportunityId"], []).append(row)
    return by_opp


class LineSync:
    def __init__(self, parent):
        self.parent = parent
        self.odoo = parent.odoo
        self.dry_run = parent.dry_run
        self.counts = parent.counts
        self.by_order = {}          # order id -> [sale.order.line rows with sf_line_id]
        self.products_code = {}     # default_code -> product id
        self.products_name = {}     # lower-case name -> product id
        self.created_products = {}  # SF code/name -> product id (or placeholder in dry run)

    # ----- reference data -----

    def load(self):
        for product in self.odoo.search_read("product.product", [], ["id", "default_code", "name"]):
            if product["default_code"]:
                self.products_code.setdefault(product["default_code"], product["id"])
            if product["name"]:
                self.products_name.setdefault(product["name"].lower(), product["id"])
        for line in self.odoo.search_read("sale.order.line", [("sf_line_id", "!=", False)], LINE_FIELDS):
            self.by_order.setdefault(m2o_id(line["order_id"]), []).append(line)

    def blocked(self, order_id, sf_line_ids):
        """§4.4: True if a delivered line of this order is gone from Salesforce."""
        for rec in self.by_order.get(order_id, []):
            if rec["sf_line_id"] not in sf_line_ids and (rec["qty_delivered"] or rec["deliverable_delivered_count"]):
                return True
        return False

    # ----- helpers -----

    def line_error(self, opp, sf_line, message):
        self.counts["errors"] += 1
        print(f"ERROR line {sf_line['Id']} on {opp['Id']} {opp['Name']}: {message}")

    def resolve_product(self, sf_line, problems):
        """Match Product2.ProductCode to default_code, then Product2.Name. Create if allowed."""
        product = sf_line.get("Product2") or {}
        code = text(product.get("ProductCode"))
        name = text(product.get("Name"))
        pid = (self.products_code.get(code) if code else None) or (
            self.products_name.get(name.lower()) if name else None)
        if pid:
            return pid

        key = code or name
        if not key:
            problems["product_id"] = "line has no product"
            return False
        if key in self.created_products:
            return self.created_products[key]
        if not config.AUTO_CREATE_PRODUCTS:
            problems["product_id"] = f"no Odoo product for {key!r}"
            return False

        if self.dry_run:
            pid = "new product"
        else:
            pid = self.odoo.create("product.product", [{
                "name": name or code,
                "default_code": code or False,
                "sale_ok": True,
                "type": "service",
                "taxes_id": [(6, 0, [])],
            }])[0]
            if code:
                self.products_code[code] = pid
            if name:
                self.products_name.setdefault(name.lower(), pid)
            self.counts["products_created"] += 1
        self.created_products[key] = pid
        self.parent.warn("product created from Salesforce (AUTO_CREATE_PRODUCTS)",
                         f"{code or '-'} {name or ''}".strip())
        return pid

    def line_vals(self, sf_line, problems):
        product = sf_line.get("Product2") or {}
        product_id = self.resolve_product(sf_line, problems)
        name = text(sf_line.get("Combined_Product_Description__c")) or text(product.get("Name")) or False
        if not name:
            problems["name"] = "line has no description"
        billing = text(sf_line.get("Billing_Schedule__c"))
        info_only = bool(sf_line.get("Information_Only__c"))
        vals = {
            "product_id": product_id,
            "name": name,
            "product_uom_qty": float(sf_line.get("Quantity") or 0),
            "price_unit": 0.0 if info_only else float(sf_line.get("Total_Billable_Amount__c") or 0),
            "discount": 0.0,
            "year_of_study": text(sf_line.get("Year_of_Study__c")),
            "country_region": text(sf_line.get("Country_Region__c")),
            "version": text(sf_line.get("Version__c")),
            "item_description": text(sf_line.get("Study_Notes_Additional_Description__c")),
            "sf_data_vendor": text(sf_line.get("Data_Vendor_netnew__c")),
            "sf_coverage_period": text(sf_line.get("Coverage_Period__c")),
            "sf_study_type": text(sf_line.get("StudyType__c")),
            "sf_study_name": text(sf_line.get("Study_Name_Formula__c")),
            "sf_wave_period": text(sf_line.get("Wave_Period__c")),
            "sf_delivery": DELIVERY_MAP.get(sf_line.get("Delivery__c")) or False,
            "sf_is_upsell": bool(sf_line.get("Up_Sell__c")),
            "sf_information_only": info_only,
            "sf_on_hold": bool(sf_line.get("On_Hold__c")),
            "sf_term_start_date": sf_line.get("Term_Start_Date__c") or False,
            "sf_term_end_date": sf_line.get("Term_End_Date__c") or False,
            "sf_billing_schedule": False if billing in BILLING_PLACEHOLDERS else (billing or False),
            "sf_removed_in_sf": False,
        }
        for field in problems:
            vals.pop(field, None)
        return vals

    def apply_locks(self, rec, vals, opp, sf_line):
        """§4.4 locked-field rules. A locked field that would change is dropped and reported."""
        delivered = max(rec["qty_delivered"] or 0, rec["deliverable_delivered_count"] or 0)
        invoiced = rec["qty_invoiced"] or 0

        def changes(field):
            return field in vals and bool(diff(rec, {field: vals[field]}, LINE_M2O))

        def reject(field, why):
            vals.pop(field, None)
            self.line_error(opp, sf_line, f"{field} locked ({why}); not applied")

        if changes("product_uom_qty") and vals["product_uom_qty"] < delivered:
            reject("product_uom_qty", f"below delivered quantity {show(delivered)}")
        if invoiced and changes("price_unit"):
            reject("price_unit", "line is invoiced")
        if invoiced and changes("sf_billing_schedule"):
            reject("sf_billing_schedule", "line is invoiced")
        if (delivered or invoiced) and changes("name"):
            reject("name", "line is delivered or invoiced")

    # ----- run -----

    def sync(self, opp, order_id, sf_lines):
        """Create, update and remove lines of one order. order_id is None only in a dry run for a
        new order, where the lines are counted but not created."""
        existing = {rec["sf_line_id"]: rec for rec in self.by_order.get(order_id, [])} if order_id else {}
        sf_ids = {line["Id"] for line in sf_lines}
        created, updated, removed = [], [], []

        for sf_line in sf_lines:
            sf_id = sf_line["Id"]
            rec = existing.get(sf_id)
            problems = {}
            vals = self.line_vals(sf_line, problems)
            if problems and rec is None:
                self.line_error(opp, sf_line, "; ".join(problems.values()) + " (line not created)")
                continue
            if rec is None:
                created.append(sf_id)
                if not self.dry_run:
                    vals.update({"order_id": order_id, "sf_line_id": sf_id})
                    self.odoo.create("sale.order.line", [vals])
                self.counts["lines_created"] += 1
                continue

            for field, message in problems.items():
                self.line_error(opp, sf_line, f"{message} (not applied)")
            self.apply_locks(rec, vals, opp, sf_line)
            changes = diff(rec, vals, LINE_M2O)
            if not changes:
                self.counts["lines_unchanged"] += 1
                continue
            updated.append(sf_id)
            if not self.dry_run:
                self.odoo.write("sale.order.line", [rec["id"]], {f: w for f, _, w in changes})
            self.counts["lines_updated"] += 1

        # §4.4: lines no longer in Salesforce. Delivered ones were blocked before the order was touched.
        for sf_id, rec in existing.items():
            if sf_id in sf_ids:
                continue
            if rec["product_uom_qty"] == 0 and rec["sf_removed_in_sf"]:
                continue
            removed.append(sf_id)
            if not self.dry_run:
                self.odoo.write("sale.order.line", [rec["id"]], {"product_uom_qty": 0.0, "sf_removed_in_sf": True})
            self.counts["lines_removed"] += 1

        if created or updated or removed:
            print(f"lines {opp['Id']} {opp['Name']}: create {len(created)}, update {len(updated)}, "
                  f"remove {len(removed)}")
