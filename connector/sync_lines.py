"""Sync Salesforce OpportunityLineItems to sale.order.line, for orders synced by sync_orders.

Mapping: docs/field-mapping.md §3 and §4.4. Upsert key: sale.order.line.sf_line_id.

Removed lines (§4.4): an Odoo line whose SF line is gone gets product_uom_qty = 0 and
sf_removed_in_sf = True. If it was delivered in any quantity, the whole order fails and
sync_orders changes nothing on it (see LineSync.blocked).

Used by sync_orders.OrderSync, which passes itself in as `parent` (for counts, warnings and errors).
"""
import html

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


def _decimals(value):
    """Decimal places from Odoo; anything that is not a whole number (or is a bool) means 2."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 2


def qty_text(value):
    return "-" if value in (None, False) else f"{value:g}"


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


def hold_name_changes(changes):
    """Split the line changes into (to write, held). With SF_HOLD_NAME_UPDATES true, a change to the line name is
    held: it is returned in held, for the log, and is not written to Odoo. Other changes are unaffected."""
    if not config.SF_HOLD_NAME_UPDATES:
        return changes, []
    held = [change for change in changes if change[0] == "name"]
    return [change for change in changes if change[0] != "name"], held


class LineSync:
    def __init__(self, parent):
        self.parent = parent
        self.odoo = parent.odoo
        self.dry_run = parent.dry_run
        self.counts = parent.counts
        self.by_order = {}          # order id -> [sale.order.line rows with sf_line_id]
        self.products_code = {}     # default_code -> product.product id
        self.products_name = {}     # lower-case name -> product.product id
        self.products_sf = {}       # sf_product_id (Product2.Id) -> product.product id
        self.product_info = {}      # product.product id -> {"tmpl", "sf", "name"}
        self.created_products = {}  # Salesforce key -> product id (or placeholder in dry run)
        self.product_outcome = {}   # Salesforce key -> "id" / "name" / "created" / "missing", counted once

    # ----- reference data -----

    def load(self):
        stamped = {t["id"]: t["sf_product_id"] or None for t in self.odoo.search_read(
            "product.template", [], ["id", "sf_product_id"])}
        for product in self.odoo.search_read("product.product", [], ["id", "default_code", "name", "product_tmpl_id"]):
            tmpl = m2o_id(product["product_tmpl_id"])
            sf_id = stamped.get(tmpl)
            self.product_info[product["id"]] = {"tmpl": tmpl, "sf": sf_id, "name": product["name"] or ""}
            if sf_id:
                self.products_sf.setdefault(sf_id, product["id"])
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
        # Lines have no error field in Salesforce, so the message goes on the opportunity.
        self.parent.issues[opp["Id"]].append(f"line {sf_line['Id']}: {message}")
        print(f"ERROR line {sf_line['Id']} on {opp['Id']} {opp['Name']}: {message}")

    def resolve_product(self, sf_line, problems):
        """Odoo product for one Salesforce line, or False with problems["product_id"] set. Order (§3):
        1. The product whose sf_product_id is Product2.Id.
        2. Only when no product has that Id: the same product code (default_code), then name. A product
           found this way is stamped with the Id, so the next run matches it by Id. A product already
           stamped with another Id is not used.
        3. Otherwise, with AUTO_CREATE_PRODUCTS, a product is created with the Id. Without it the line
           cannot be synced, and its order is not synced (sync_orders.OrderSync.create_or_update)."""
        product = sf_line.get("Product2") or {}
        sf_id = text(sf_line.get("Product2Id"))
        code = text(product.get("ProductCode"))
        name = text(product.get("Name"))
        if not sf_id and not code and not name:
            problems["product_id"] = "line has no product"
            return False
        label = name or code or "unnamed product"
        detail = label + (f", code {code}" if code else "") + (f", Salesforce {sf_id}" if sf_id else "")
        key = sf_id or code or name

        if sf_id and sf_id in self.products_sf:
            self.outcome(key, "id")
            return self.products_sf[sf_id]
        if key in self.created_products:
            return self.created_products[key]
        pid, reason = self.fallback_match(code, name, sf_id)
        if pid:
            self.stamp(pid, sf_id)
            self.outcome(key, "name")
            return pid
        if not config.AUTO_CREATE_PRODUCTS:
            self.outcome(key, "missing")
            problems["product_id"] = f"product not in Odoo: {detail}" + (f" ({reason})" if reason else "")
            return False

        pid = self.create_product(code, name, sf_id)
        self.created_products[key] = pid
        self.outcome(key, "created")
        self.parent.warn("product created from Salesforce (AUTO_CREATE_PRODUCTS)", detail)
        return pid

    def fallback_match(self, code, name, sf_id):
        """(product id, None) for the code, then name, match. (None, reason) when the only match is a
        product already stamped with another Salesforce Id, or when there is no match (reason None)."""
        candidates = []
        if code and code in self.products_code:
            candidates.append(self.products_code[code])
        if name and name.lower() in self.products_name:
            candidates.append(self.products_name[name.lower()])
        reason = None
        for pid in candidates:
            linked = self.product_info[pid]["sf"]
            if linked and linked != sf_id:
                reason = f"Odoo product {self.product_info[pid]['name']!r} is linked to another Salesforce product"
                continue
            return pid, None
        return None, reason

    def stamp(self, pid, sf_id):
        """Set sf_product_id on the product's template, so the next run matches it by Id."""
        info = self.product_info[pid]
        if not sf_id or info["sf"] == sf_id:
            return
        if not self.dry_run:
            self.odoo.write("product.template", [info["tmpl"]], {"sf_product_id": sf_id})
        info["sf"] = sf_id
        self.products_sf[sf_id] = pid
        self.counts["products_stamped"] += 1
        verb = "[dry-run] would stamp" if self.dry_run else "stamped"
        print(f"{verb} product {info['name']!r} with Salesforce Id {sf_id}")

    def create_product(self, code, name, sf_id):
        """Create the product (service, no tasks, no taxes) with the Salesforce Id. Dry run: a placeholder."""
        if self.dry_run:
            return "new product"
        vals = {
            "name": name or code,
            "default_code": code or False,
            "sale_ok": True,
            "type": "service",
            "service_tracking": "no",  # never create tasks or projects from these products
            "taxes_id": [(6, 0, [])],
        }
        if sf_id:
            vals["sf_product_id"] = sf_id
        pid = self.odoo.create("product.product", [vals])[0]
        tmpl = m2o_id(self.odoo.search_read("product.product", [("id", "=", pid)], ["product_tmpl_id"])[0]["product_tmpl_id"])
        self.product_info[pid] = {"tmpl": tmpl, "sf": sf_id, "name": name or code or ""}
        if sf_id:
            self.products_sf[sf_id] = pid
        if code:
            self.products_code.setdefault(code, pid)
        if name:
            self.products_name.setdefault(name.lower(), pid)
        return pid

    def outcome(self, key, kind):
        """Count one Salesforce product once per run: matched by Id, matched by name (stamped), created,
        or missing (not in Odoo, AUTO_CREATE_PRODUCTS off)."""
        if key in self.product_outcome:
            return
        self.product_outcome[key] = kind
        self.counts[{"id": "products_matched_id", "name": "products_matched_name",
                     "created": "products_created", "missing": "products_missing"}[kind]] += 1

    def unknown_products(self, sf_lines):
        """One message per line whose product cannot be found or created. Empty when every product resolves.
        Resolves products before any Odoo write for the order, so a failing order writes nothing."""
        messages = []
        for sf_line in sf_lines:
            problems = {}
            if not self.resolve_product(sf_line, problems):
                messages.append(problems["product_id"])
        return list(dict.fromkeys(messages))

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

    def sf_product_name(self, sf_line):
        product = sf_line.get("Product2") or {}
        return text(product.get("Name")) or text(sf_line.get("Combined_Product_Description__c")) or "Line"

    def odoo_product_name(self, rec):
        product = rec.get("product_id")
        return product[1] if isinstance(product, (list, tuple)) and len(product) > 1 else rec.get("name") or "Line"

    def currency_info(self, code):
        cache = self.__dict__.setdefault("_currency_info", {})
        if code not in cache:
            rows = self.odoo.search_read("res.currency", [("name", "=", code or "")],
                                         ["symbol", "decimal_places", "position"])
            if rows:
                r = rows[0]
                cache[code] = {
                    "symbol": r["symbol"] or "",
                    "dp": _decimals(r["decimal_places"]),
                    "position": r["position"] or "before",
                }
            else:
                cache[code] = {"symbol": f"{code} " if code else "", "dp": 2, "position": "before"}
        return cache[code]

    def money(self, value, code):
        """An amount in the order currency, formatted with the currency's symbol and decimals."""
        info = self.currency_info(code)
        number = "-" if value in (None, False) else f"{value:,.{info['dp']}f}"
        if info["position"] == "after":
            return f"{number} {info['symbol']}"
        return f"{info['symbol']}{number}"

    def changed_text(self, rec, changes, sf_line, currency):
        """One sentence for an updated line, or None if no Finance/CS-facing field changed. A description
        change (the line name, from Salesforce) is listed as "description: old → new"."""
        by_field = {f: (c, w) for f, c, w in changes}
        clauses = []
        if "product_id" in by_field:
            old = rec["product_id"][1] if isinstance(rec["product_id"], (list, tuple)) else "-"
            clauses.append(f"product {old} → {self.sf_product_name(sf_line)}")
        if "name" in by_field:
            c, w = by_field["name"]
            clauses.append(f"description: {show(c)} → {show(w)}")
        if "product_uom_qty" in by_field:
            c, w = by_field["product_uom_qty"]
            clauses.append(f"quantity {qty_text(c)} → {qty_text(w)}")
        if "price_unit" in by_field:
            c, w = by_field["price_unit"]
            clauses.append(f"price {self.money(c, currency)} → {self.money(w, currency)}")
        if "sf_on_hold" in by_field:
            clauses.append("On Hold set" if by_field["sf_on_hold"][1] else "On Hold cleared")
        if "sf_removed_in_sf" in by_field and not by_field["sf_removed_in_sf"][1]:
            clauses.append("back in Salesforce")
        if not clauses:
            return None
        return f"{self.sf_product_name(sf_line)} — " + ", ".join(clauses) + "."

    def added_text(self, sf_line, vals, currency):
        return (f"{self.sf_product_name(sf_line)} (qty {qty_text(vals['product_uom_qty'])} "
                f"@ {self.money(vals['price_unit'], currency)})")

    def post_note(self, opp, order_id, parts):
        """One internal note on the order listing the Finance/CS-facing changes of this run."""
        body = "Salesforce changes: " + " ".join(parts)
        self.counts["line_notes_posted"] += 1
        if self.dry_run:
            print(f"[dry-run] would post line note on {opp['Id']} {opp['Name']}: {body}")
            return
        self.odoo.call("sale.order", "message_post", ids=[order_id], body=html.escape(body),
                       message_type="comment", subtype_xmlid="mail.mt_note")

    def removal_candidates(self, order_id, sf_ids):
        """Odoo lines of the order whose Salesforce line is no longer listed and that are not already
        removed (§4.4). These are the removals the brake counts."""
        return [rec for rec in self.by_order.get(order_id, [])
                if rec["sf_line_id"] not in sf_ids
                and not (rec["product_uom_qty"] == 0 and rec["sf_removed_in_sf"])]

    def sync(self, opp, order_id, sf_lines, new_order=False, allow_removals=True):
        """Create, update and remove lines of one order. order_id is None only in a dry run for a
        new order, where the lines are counted but not created. Changes to an existing order are
        posted as ONE note per run; a new order gets no note. allow_removals False (the removal brake)
        skips the removals of this order; creates and updates still apply. A held line name (see
        hold_name_changes) is logged and not written, and is not in the note."""
        existing = {rec["sf_line_id"]: rec for rec in self.by_order.get(order_id, [])} if order_id else {}
        sf_ids = {line["Id"] for line in sf_lines}
        currency = opp.get("CurrencyIsoCode")
        created, updated, removed = [], [], []
        update_parts, removed_parts, added_parts = [], [], []

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
                added_parts.append(self.added_text(sf_line, vals, currency))
                continue

            for field, message in problems.items():
                self.line_error(opp, sf_line, f"{message} (not applied)")
            self.apply_locks(rec, vals, opp, sf_line)
            changes, held = hold_name_changes(diff(rec, vals, LINE_M2O))
            for _, current, wanted in held:
                self.counts["names_held"] += 1
                print(f"name held (SF_HOLD_NAME_UPDATES) line {sf_id} on {opp['Id']} {opp['Name']}: "
                      f"{show(current)} -> {show(wanted)}")
            if not changes:
                self.counts["lines_unchanged"] += 1
                continue
            updated.append(sf_id)
            if not self.dry_run:
                self.odoo.write("sale.order.line", [rec["id"]], {f: w for f, _, w in changes})
            self.counts["lines_updated"] += 1
            part = self.changed_text(rec, changes, sf_line, currency)
            if part:
                update_parts.append(part)

        # §4.4: lines no longer in Salesforce. Delivered ones were blocked before the order was touched.
        # The brake (allow_removals False) leaves them as they are.
        if allow_removals:
            for rec in self.removal_candidates(order_id, sf_ids):
                removed.append(rec["sf_line_id"])
                if not self.dry_run:
                    self.odoo.write("sale.order.line", [rec["id"]], {"product_uom_qty": 0.0, "sf_removed_in_sf": True})
                self.counts["lines_removed"] += 1
                removed_parts.append(f"{self.odoo_product_name(rec)} — removed in Salesforce (quantity → 0).")

        if created or updated or removed:
            print(f"lines {opp['Id']} {opp['Name']}: create {len(created)}, update {len(updated)}, "
                  f"remove {len(removed)}")
        parts = update_parts + removed_parts
        if added_parts:
            parts.append("Added: " + "; ".join(added_parts) + ".")
        if parts and not new_order:
            self.post_note(opp, order_id, parts)

