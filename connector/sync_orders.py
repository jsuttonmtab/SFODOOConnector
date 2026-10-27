"""Sync Salesforce Opportunities into Odoo sales orders (sale.order). Headers only; no order lines yet.

Mapping: docs/field-mapping.md §2, §4.1, §4.3, §4.4, §4.6.
Upsert key: sale.order.sf_opportunity_id. Orders are created as drafts and never confirmed.
Salesforce is read only. Nothing is written back.

    python -m connector.sync_orders --dry-run   # report what would change, write nothing
    python -m connector.sync_orders             # create and update in Odoo
"""
import argparse
import sys
from collections import Counter
from datetime import date

from . import config
from .odoo_client import OdooClient, OdooError, m2o_id
from .sf_client import SalesforceClient, SalesforceError, get_session
from .sync_lines import LineSync, fetch_sf_lines
from .sync_accounts import (
    INVALID_EMAIL_SUFFIX, PENDING, AccountSync, build_parent_soql, comment_html,
    diff, fetch_parent_chain, plain_text, show, soql_literal, text,
)

STAGE_AWARD_WON = config.STAGE_AWARD_WON
STAGE_CLOSED = "Closed"
PLACEHOLDER_NEW_INVOICE_ADDRESS = "new invoice address"
REVIEW_ACTIVITY_SUMMARY = "Salesforce stage left Award Won: review order"

OPP_FIELDS = [
    "Id", "Name", "AccountId", "Account.Name", "StageName", "CurrencyIsoCode", "Owner.Email",
    "Date_Award_Won_Date__c", "Term_Start_Date__c", "Type", "Description", "Invoice_Notes__c",
    "Additional_information_to_invoice__c", "VAT_Tax_Number__c", "Billing_Distribution_Method__c",
    "PO_Number__c", "Payment_Terms__c", "mTab_Billing_Entity__c", "Revenue_allocation_company_ID__c",
    "Billing_Street__c", "Billing_City__c", "Billing_State_Province__c", "Billing_Zip_Postal_Code__c",
    "Billing_Country__c",
]

OPP_TYPES = {"New": "new", "Up-Sell": "up_sell", "Cross-Sell": "cross_sell", "Renewal": "renewal"}

ORDER_FIELDS = [
    "id", "sf_opportunity_id", "sf_stage", "partner_id", "company_id", "pricelist_id", "payment_term_id",
    "user_id", "partner_invoice_id", "origin", "client_order_ref", "note", "sf_award_won_date",
    "sf_term_start_date", "sf_opportunity_type", "sf_description", "sf_invoice_notes",
    "sf_invoice_additional_info", "sf_billing_distribution_method", "sf_vat_number",
    "sf_revenue_allocation_company_id", "project_id",
]
ORDER_M2O = {"partner_id", "company_id", "pricelist_id", "payment_term_id", "user_id",
             "partner_invoice_id", "sf_revenue_allocation_company_id", "project_id"}
# §4.4 header fields: once the order exists, changes are errors and are not applied.
LOCKED = ("partner_id", "company_id", "pricelist_id", "sf_award_won_date")


def stage_list_soql():
    return f"({soql_literal(STAGE_AWARD_WON)}, {soql_literal(STAGE_CLOSED)})"


def _account_name_filter():
    names = ", ".join(soql_literal(n) for n in sorted(config.TEST_ACCOUNT_NAMES))
    return f"AND Account.Name NOT IN ({names})" if names else ""


def build_opp_soql(where):
    return f"""SELECT {', '.join(OPP_FIELDS)}
FROM Opportunity
WHERE celigo_sfnsio__Test_Mode_Record__c = false
AND Account.celigo_sfnsio__Test_Mode_Record__c = false
{_account_name_filter()}
AND {where}"""


def query_opps_by_id(sf, ids):
    found = []
    ids = sorted(ids)
    for i in range(0, len(ids), 200):
        id_list = ", ".join(soql_literal(x) for x in ids[i:i + 200])
        found += sf.query(build_opp_soql(f"Id IN ({id_list})"))
    return found


def address_key(street, city, zip_code, state_id, country_id):
    return ((street or "").lower(), (city or "").lower(), (zip_code or "").lower(),
            state_id or False, country_id or False)


class OrderSync(AccountSync):
    def __init__(self, odoo, dry_run):
        super().__init__(odoo, dry_run)
        self.counts = Counter()
        self.sf_lines = {}  # opportunity Id -> OpportunityLineItems
        self.lines = None

    # ----- reference data -----

    def load_orders(self):
        rows = self.odoo.search_read("sale.order", [("sf_opportunity_id", "!=", False)], ORDER_FIELDS)
        self.orders = {row["sf_opportunity_id"]: row for row in rows}

    def load_terms(self):
        self.terms = {t["name"]: t["id"] for t in
                      self.odoo.search_read("account.payment.term", [], ["id", "name"])}

    def load_partners(self):
        rows = self.odoo.search_read("res.partner", [("sf_account_id", "!=", False)], ["id", "sf_account_id"])
        self.partners = {row["sf_account_id"]: row["id"] for row in rows}

    def load_invoice_addresses(self):
        rows = self.odoo.search_read(
            "res.partner", [("type", "=", "invoice")],
            ["id", "parent_id", "street", "city", "zip", "state_id", "country_id"],
        )
        self.invoice_addresses = {
            (m2o_id(r["parent_id"]), address_key(r["street"], r["city"], r["zip"],
                                                 m2o_id(r["state_id"]), m2o_id(r["country_id"]))): r["id"]
            for r in rows if r["parent_id"]
        }

    # ----- field mapping -----

    def opp_user(self, opp):
        email = text((opp.get("Owner") or {}).get("Email"))
        if not email:
            return False
        email = INVALID_EMAIL_SUFFIX.sub("", email)
        user = self.users.get(email.lower())
        if not user:
            self.warn("no Odoo user for opportunity owner (user left blank)", email, opp)
        return user or False

    def opp_terms(self, opp, problems):
        raw = text(opp.get("Payment_Terms__c"))
        if not raw or raw in config.PLACEHOLDER_VALUES:
            problems["payment_term_id"] = "payment terms not set"
            return False
        term = self.terms.get(raw)
        if not term:
            problems["payment_term_id"] = f"payment terms {raw!r} not in Odoo"
        return term or False

    def opp_company(self, opp, problems):
        raw = text(opp.get("mTab_Billing_Entity__c"))
        if not raw or raw in config.PLACEHOLDER_VALUES:
            problems["company_id"] = "billing entity not set"
            return False
        company = self.billing_entity_id(opp)
        if not company:
            problems["company_id"] = f"billing entity {raw!r} not resolved"
        return company

    def opp_type(self, opp):
        raw = text(opp.get("Type"))
        if not raw:
            return False
        value = OPP_TYPES.get(raw)
        if not value:
            self.warn("unknown opportunity type (left blank)", repr(raw), opp)
        return value or False

    def revenue_company(self, opp):
        sf_id = text(opp.get("Revenue_allocation_company_ID__c"))
        if not sf_id:
            return False
        partner = self.partners.get(sf_id)
        if not partner:
            self.warn("revenue allocation account not in Odoo (left blank)", sf_id, opp)
        return partner or False

    def invoice_partner(self, opp, partner_id):
        """Find or create the type='invoice' child for the opportunity's Billing_* address."""
        street = text(opp.get("Billing_Street__c"))
        city = text(opp.get("Billing_City__c"))
        state_raw = text(opp.get("Billing_State_Province__c"))
        zip_code = text(opp.get("Billing_Zip_Postal_Code__c"))
        country_raw = text(opp.get("Billing_Country__c"))
        if not any([street, city, state_raw, zip_code, country_raw]):
            return partner_id  # no separate invoice address: invoice the customer

        country_id = self.addr.country_id(country_raw)
        if not country_id:
            self.warn("opportunity billing country missing or not matched (invoices need it)",
                      repr(country_raw), opp)
        state_id = self.addr.state_id(country_id, state_raw) if state_raw else None
        if state_raw and not state_id:
            self.warn("opportunity billing state not matched (left blank)", repr(state_raw), opp)

        key = (partner_id, address_key(street, city, zip_code, state_id, country_id))
        if key in self.invoice_addresses:
            return self.invoice_addresses[key]
        if self.dry_run:
            return PLACEHOLDER_NEW_INVOICE_ADDRESS
        child = {
            "parent_id": partner_id, "type": "invoice", "is_company": False,
            "name": (opp.get("Account") or {}).get("Name") or opp["Name"],
            "street": street, "city": city, "zip": zip_code,
            "state_id": state_id or False, "country_id": country_id or False,
        }
        new_id = self.odoo.create("res.partner", [child])[0]
        self.invoice_addresses[key] = new_id
        self.counts["invoice_addresses_created"] += 1
        return new_id

    def order_vals(self, opp, partner_id):
        """Desired sale.order values for an opportunity. Returns (vals, problems).

        problems maps each field that could not be resolved to a message. Those fields
        are left out of vals, so an update never writes a bad value.
        """
        problems = {}
        vals = {
            "origin": text(opp.get("Name")),
            "client_order_ref": text(opp.get("PO_Number__c")),
            "note": comment_html(opp.get("Additional_information_to_invoice__c")),
            "sf_award_won_date": text(opp.get("Date_Award_Won_Date__c")),
            "sf_term_start_date": text(opp.get("Term_Start_Date__c")),
            "sf_opportunity_type": self.opp_type(opp),
            "sf_description": text(opp.get("Description")),
            "sf_invoice_notes": text(opp.get("Invoice_Notes__c")),
            "sf_invoice_additional_info": text(opp.get("Additional_information_to_invoice__c")),
            "sf_billing_distribution_method": text(opp.get("Billing_Distribution_Method__c")),
            "sf_vat_number": text(opp.get("VAT_Tax_Number__c")),
            "sf_stage": opp["StageName"],
            "sf_revenue_allocation_company_id": self.revenue_company(opp),
            "company_id": self.opp_company(opp, problems),
            "payment_term_id": self.opp_terms(opp, problems),
            "user_id": self.opp_user(opp),
        }
        if not text(opp.get("CurrencyIsoCode")):
            problems["pricelist_id"] = "no currency"
        vals["pricelist_id"] = self.pricelist_for(text(opp.get("CurrencyIsoCode")), vals.get("company_id") or None)
        if partner_id is PENDING_PARTNER:
            pass  # dry run: the customer is created by the account sync in this run
        elif partner_id:
            vals["partner_id"] = partner_id
            vals["partner_invoice_id"] = self.invoice_partner(opp, partner_id)
        else:
            problems["partner_id"] = "account not in Odoo (account sync did not create it)"
        for field in problems:
            vals.pop(field, None)
        if "partner_id" in problems:
            vals.pop("partner_invoice_id", None)
        return vals, problems

    # ----- output helpers -----

    def report_order(self, action, opp, detail=""):
        self.report(action, {"Id": opp["Id"], "Name": opp["Name"]}, detail)

    def order_error(self, opp, message):
        self.counts["errors"] += 1
        print(f"ERROR {opp['Id']} {opp['Name']}: {message}")

    # ----- run -----

    def run(self, opps):
        self.lines = LineSync(self)
        self.lines.load()
        self.load_terms()
        self.load_partners()
        self.load_invoice_addresses()
        self.check_currencies(opps)

        seen = set()
        for opp in opps:
            if opp["Id"] in seen:
                continue
            seen.add(opp["Id"])
            stage = opp["StageName"]
            existing = self.orders.get(opp["Id"])
            try:
                if stage == STAGE_AWARD_WON:
                    self.create_or_update(opp, existing)
                elif stage == STAGE_CLOSED:
                    if existing:
                        self.create_or_update(opp, existing)
                    else:
                        self.counts["closed_not_in_odoo"] += 1
                elif existing:
                    self.stage_reversal(opp, existing)
            except OdooError as err:
                self.order_error(opp, err)

        self.summary()

    def create_or_update(self, opp, existing):
        sf_lines = self.sf_lines.get(opp["Id"], [])
        if existing and self.lines.blocked(existing["id"], {line["Id"] for line in sf_lines}):
            self.order_error(opp, "a delivered line was removed in Salesforce; order not changed (§4.4)")
            return
        if existing and existing.get("sf_stage") != STAGE_AWARD_WON and opp["StageName"] == STAGE_AWARD_WON:
            self.note_back_at_award_won(opp, existing)
        ok, order_id = self.sync_header(opp, existing)
        if ok:
            self.lines.sync(opp, order_id, sf_lines, new_order=existing is None)

    def note_back_at_award_won(self, opp, existing):
        """Mark on the order that its opportunity is back at Award Won. Notes for a later reversal
        count only if posted after the latest of these markers, so every actual reversal gets one note.
        Posted before the header is written: if this fails, the header is retried on the next run."""
        old = show(existing.get("sf_stage"))
        body = f"Salesforce stage back to {STAGE_AWARD_WON} (was {old})."
        self.report_order("post back-at-Award-Won note", opp, f"{old} -> {STAGE_AWARD_WON}")
        if not self.dry_run:
            self.odoo.call("sale.order", "message_post", ids=[existing["id"]], body=body,
                           message_type="comment", subtype_xmlid="mail.mt_note")
        self.counts["back_notes_posted"] += 1

    def sync_header(self, opp, existing):
        """Create or update the order header. Returns (ok, order_id). order_id is None for a
        new order in a dry run. When ok is False, the order's lines are not synced."""
        account_id = text(opp.get("AccountId"))
        partner_id = self.partners.get(account_id) if account_id else None
        if not partner_id and self.dry_run:
            partner_id = PENDING_PARTNER
        vals, problems = self.order_vals(opp, partner_id)

        if existing is None:
            if problems:
                self.order_error(opp, "not created: " + "; ".join(problems.values()))
                return False, None
            vals["state"] = "draft"
            vals["sf_opportunity_id"] = opp["Id"]
            if vals.get("sf_award_won_date"):
                vals["date_order"] = f"{vals['sf_award_won_date']} 00:00:00"
            project_id = self.project_for_order(m2o_id(vals.get("company_id")), opp)
            if project_id:
                vals["project_id"] = project_id
            customer = "created in this run" if partner_id is PENDING_PARTNER else show(vals.get("partner_id"))
            detail = f"customer {customer}" + (", project set" if project_id else "")
            self.report_order("create", opp, detail)
            order_id = None
            if not self.dry_run:
                order_id = self.odoo.create("sale.order", [vals])[0]
            self.counts["created"] += 1
            return True, order_id

        # Locked header fields (§4.4): report and do not apply.
        for field in LOCKED:
            if field in vals:
                changed = diff(existing, {field: vals[field]}, ORDER_M2O)
                if changed:
                    _, current, wanted = changed[0]
                    self.order_error(opp, f"locked field {field} changed {show(current)} -> {show(wanted)}; not applied")
                    del vals[field]
        for problem in problems.values():
            self.order_error(opp, f"not applied: {problem}")

        # Project: set only when the order has none, so a project chosen by hand is kept.
        if existing.get("project_id"):
            self.counts["projects_already_set"] += 1
        else:
            project_id = self.project_for_order(m2o_id(existing.get("company_id")), opp)
            if project_id:
                vals["project_id"] = project_id

        changes = diff(existing, vals, ORDER_M2O)
        if not changes:
            self.counts["unchanged"] += 1
            return True, existing["id"]
        detail = ", ".join(f"{f}: {show(c)} -> {show(w)}" for f, c, w in changes)
        self.report_order("update", opp, detail)
        if not self.dry_run:
            self.odoo.write("sale.order", [existing["id"]], {f: w for f, _, w in changes})
        self.counts["updated"] += 1
        return True, existing["id"]

    def resolve_project(self):
        """DELIVERABLES_PROJECT as an active, billable project: (id, company id or None), or None.
        Resolved once per run. A problem is logged once, and project changes are skipped."""
        if hasattr(self, "_project"):
            return self._project
        self._project = None
        name = config.DELIVERABLES_PROJECT
        if not name:
            self.warn("DELIVERABLES_PROJECT is empty; orders' project not set", "")
            return None
        rows = self.odoo.search_read("project.project", [("name", "=", name), ("active", "in", [True, False])],
                                     ["id", "active", "allow_billable", "company_id"])
        if not rows:
            self.warn("DELIVERABLES_PROJECT not found in Odoo; orders' project not set", name)
            return None
        active = sorted((r for r in rows if r["active"]), key=lambda r: r["id"])
        if not active:
            self.warn("DELIVERABLES_PROJECT is archived; orders' project not set", name)
            return None
        if len(active) > 1:
            self.warn("several active projects have this name; lowest id used", f"{name}: {len(active)}")
        project = active[0]
        if not project["allow_billable"]:
            self.warn("DELIVERABLES_PROJECT is not billable; orders' project not set", name)
            return None
        self._project = {"id": project["id"], "company": m2o_id(project["company_id"])}
        return self._project

    def project_for_order(self, company_id, opp):
        """Project id to set on an order, or None (skipped, and counted). A project that belongs to
        another company is not set, so the order does not cause a multi-company error."""
        project = self.resolve_project()
        if not project:
            self.counts["projects_skipped"] += 1
            return None
        if project["company"] and project["company"] != company_id:
            self.warn("deliverables project belongs to another company; project not set on this order",
                      f"project company {project['company']}, order company {company_id or '-'}", opp)
            self.counts["projects_skipped"] += 1
            return None
        self.counts["projects_set"] += 1
        return project["id"]

    def stage_reversal(self, opp, existing):
        """§4.6: the opportunity left Award Won. The order is not changed. Post a note, add a To Do
        for the Finance user, then record the new stage. Acts only when the stage changed since the
        last run: an unchanged reversed order is counted silently and nothing is warned. Each step
        checks Odoo first, so a run that failed part-way finishes on the next run without duplicates."""
        new_stage = opp["StageName"]
        old_stage = existing.get("sf_stage")
        if old_stage == new_stage:
            self.counts["reversed_unchanged"] += 1
            return
        self.warn("stage moved out of Award Won (order not changed, review in Odoo)",
                  f"{show(old_stage)} -> {new_stage}", opp)
        self.counts["stage_reversals"] += 1

        body = (f"Salesforce stage changed from {show(old_stage)} to {new_stage}. "
                "The order was not changed; please review it.")
        if self.note_posted(existing["id"], show(old_stage), new_stage):
            self.counts["notes_already_posted"] += 1
        else:
            self.report_order("post note", opp, f"{show(old_stage)} -> {new_stage}")
            if not self.dry_run:
                self.odoo.call("sale.order", "message_post", ids=[existing["id"]], body=body,
                               message_type="comment", subtype_xmlid="mail.mt_note")
            self.counts["notes_posted"] += 1
        self.review_activity(opp, existing["id"], body)

        self.report_order("update sf_stage", opp, f"{show(old_stage)} -> {new_stage}")
        if not self.dry_run:
            self.odoo.write("sale.order", [existing["id"]], {"sf_stage": new_stage})

    def last_return_id(self, order_id):
        """Id of the latest 'back at Award Won' marker on the order, or 0 if there is none."""
        rows = self.odoo.search_read("mail.message", [
            ("model", "=", "sale.order"),
            ("res_id", "=", order_id),
            ("body", "ilike", "Salesforce stage back to"),
        ], ["id"])
        return max((r["id"] for r in rows), default=0)

    def note_posted(self, order_id, old_stage, new_stage):
        """True if this reversal's note is already on the order. Only notes posted after the order
        last returned to Award Won count, so a repeat reversal to the same stage gets its own note."""
        rows = self.odoo.search_read("mail.message", [
            ("model", "=", "sale.order"),
            ("res_id", "=", order_id),
            ("id", ">", self.last_return_id(order_id)),
            ("body", "ilike", f"from {old_stage} to {new_stage}. The order was not changed"),
        ], ["id"])
        return bool(rows)

    def open_review_activity(self, order_id):
        """True if an open To Do with the connector's summary is already on the order."""
        order = self.odoo.search_read("sale.order", [("id", "=", order_id)], ["activity_ids"])
        activity_ids = (order[0]["activity_ids"] or []) if order else []
        if not activity_ids:
            return False
        rows = self.odoo.search_read("mail.activity", [
            ("id", "in", activity_ids),
            ("summary", "=", REVIEW_ACTIVITY_SUMMARY),
        ], ["id"])
        return bool(rows)

    def resolve_finance_user(self):
        """Resolve FINANCE_ACTIVITY_USER once per run to an active internal user id, or None.
        A problem is logged once, not once per order."""
        if hasattr(self, "_finance_user_id"):
            return self._finance_user_id
        self._finance_user_id = None
        login = config.FINANCE_ACTIVITY_USER
        if not login:
            self.warn("FINANCE_ACTIVITY_USER not set; To Dos skipped", "")
            return None
        rows = self.odoo.search_read("res.users", [("login", "=", login), ("active", "in", [True, False])],
                                     ["id", "active", "share"])
        if not rows:
            self.warn("FINANCE_ACTIVITY_USER login not found in Odoo; To Dos skipped", login)
            return None
        user = rows[0]
        if not user["active"]:
            self.warn("FINANCE_ACTIVITY_USER is an archived user; To Dos skipped", login)
            return None
        if user["share"]:
            self.warn("FINANCE_ACTIVITY_USER is a portal user, not an internal user; To Dos skipped", login)
            return None
        if config.ODOO_API_LOGIN and login.lower() == config.ODOO_API_LOGIN.lower():
            self.warn("FINANCE_ACTIVITY_USER is the API user; Odoo will not send an assignment email", login)
        self._finance_user_id = user["id"]
        return user["id"]

    def review_activity(self, opp, order_id, body):
        """Create the To Do with the standard Odoo method, unless one is already open."""
        user_id = self.resolve_finance_user()
        if not user_id:
            self.counts["todos_skipped"] += 1
            return
        if self.open_review_activity(order_id):
            self.counts["activities_already_open"] += 1
            self.report_order("skip To Do (one is already open)", opp)
            return
        self.report_order("create To Do", opp, f"for {config.FINANCE_ACTIVITY_USER}")
        if not self.dry_run:
            self.odoo.call("sale.order", "activity_schedule", ids=[order_id],
                           act_type_xmlid="mail.mail_activity_data_todo", summary=REVIEW_ACTIVITY_SUMMARY,
                           note=body, user_id=user_id, date_deadline=date.today().isoformat())
        self.counts["activities_created"] += 1

    def summary(self):
        c = self.counts
        mode = "dry run, nothing written" if self.dry_run else "written to Odoo"
        print(f"\nInfo: {c['closed_not_in_odoo']} Closed opportunities not in Odoo (ignored); "
              f"{c['reversed_unchanged']} reversed orders with no stage change")
        self.print_warnings()
        print(
            f"\nDone ({mode}): created {c['created']}, updated {c['updated']}, unchanged {c['unchanged']}, "
            f"projects set {c['projects_set']}, already set {c['projects_already_set']}, "
            f"skipped {c['projects_skipped']}, "
            f"stage reversals {c['stage_reversals']} (notes {c['notes_posted']}, To Dos {c['activities_created']}, "
            f"already open {c['activities_already_open']}, back-at-Award-Won notes {c['back_notes_posted']}), "
            f"lines created {c['lines_created']}, updated {c['lines_updated']}, "
            f"unchanged {c['lines_unchanged']}, removed {c['lines_removed']}, "
            f"line change notes {c['line_notes_posted']}, "
            f"products created {c['products_created']}, "
            f"invoice addresses created {c['invoice_addresses_created']}, "
            f"pricelists created {c['pricelists_created']}, errors {c['errors']}"
        )


PENDING_PARTNER = PENDING  # marker for a customer that only exists in this dry run


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Sync Salesforce Opportunities to Odoo sales orders (headers and lines).")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change; write nothing to Odoo")
    parser.add_argument("--opportunity", metavar="SF_ID",
                        help="sync only this opportunity (for checks on one order)")
    args = parser.parse_args(argv)

    try:
        odoo = OdooClient(config.ODOO_URL, config.ODOO_DB, config.ODOO_API_KEY)
        sf = SalesforceClient(get_session())
        sync = OrderSync(odoo, args.dry_run)
        sync.load_reference()
        sync.load_orders()

        qualifying = sf.query(build_opp_soql(f"StageName IN {stage_list_soql()}"))
        qualifying_ids = {o["Id"] for o in qualifying}
        # Orders already in Odoo whose opportunity has left stage 07 / Closed (§4.6).
        reversal_ids = [sid for sid in sync.orders if sid not in qualifying_ids]
        reversals = query_opps_by_id(sf, reversal_ids) if reversal_ids else []
        if args.opportunity:
            qualifying = [o for o in qualifying if o["Id"] == args.opportunity]
            reversals = [o for o in reversals if o["Id"] == args.opportunity]
            if not qualifying and not reversals:
                print(f"ERROR {args.opportunity} is not a synced order candidate "
                      "(stage 07 or Closed, or an existing order whose stage changed)")
                return 1
        opps = qualifying + reversals

        # Customers must exist before their orders (§2). Sync the missing accounts first.
        sync.load_partners()
        needed = {o["AccountId"] for o in qualifying
                  if o.get("AccountId") and (o["StageName"] == STAGE_AWARD_WON or o["Id"] in sync.orders)}
        missing = sorted(needed - set(sync.partners))
        if missing:
            accounts = sf.query(build_parent_soql(missing))
            parents = fetch_parent_chain(sf, accounts)
            print(f"{len(missing)} customers missing in Odoo: running the account sync for {len(accounts)} "
                  f"accounts and {len(parents)} parents first")
            AccountSync(odoo, args.dry_run).run(accounts, parents)
            sync.load_partners()

        line_opp_ids = [o["Id"] for o in qualifying if o["StageName"] == STAGE_AWARD_WON or o["Id"] in sync.orders]
        sync.sf_lines = fetch_sf_lines(sf, line_opp_ids)
        print(f"{len(qualifying)} opportunities at stage {STAGE_AWARD_WON!r} or Closed, "
              f"{len(reversals)} existing orders whose opportunity left them")
    except (OdooError, SalesforceError) as err:
        print(f"ERROR {err}")
        return 1

    try:
        sync.run(opps)
    except OdooError as err:
        print(f"ERROR {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
