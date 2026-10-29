"""Odoo -> Salesforce write-back (phase 1): Odoo ids and links, sync errors, and line quantities.

Runs after the Odoo side of sync_accounts and sync_orders. For the records a run handled, it reads the
Odoo state, compares it with what Salesforce already holds, and writes only the fields that differ,
200 records per call (Composite sObject Collections). A failed write is reported and does not stop the
Odoo sync. Rules: docs/field-mapping.md §5.1.

Writes happen only when SF_WRITEBACK=true, and only to the org named by SF_EXPECTED_ORG_ID.
--dry-run prints the field changes per record and writes nothing; it does not need SF_WRITEBACK.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import config
from .odoo_client import OdooError
from .sf_client import SalesforceError

BATCH = 200
ERROR_MAX = 32000  # Odoo_Sync_Error__c is Long Text (32768 characters); room left for the truncation mark

# Field -> Salesforce type from describe. Account and Opportunity share the sync fields.
# Names are spelled as the org's describe returns them (Odoo_ID__c, not Odoo_Id__c).
SYNC_FIELDS = {
    "Odoo_ID__c": "double", "Odoo_Record__c": "url",
    "Odoo_Sync_Error__c": "textarea", "Odoo_Last_Sync__c": "datetime",
}
LINE_FIELDS = {
    "Odoo_Line_ID__c": "double", "Odoo_Qty_Delivered__c": "double",
    "Odoo_Qty_Invoiced__c": "double", "Odoo_Fulfilled__c": "boolean",
}
REQUIRED = {"Account": SYNC_FIELDS, "Opportunity": SYNC_FIELDS, "OpportunityLineItem": LINE_FIELDS}

ODOO_LINE_FIELDS = [
    "id", "sf_line_id", "product_uom_qty", "qty_delivered", "qty_invoiced",
    "deliverable_count", "deliverable_delivered_count",
]
TEXT = {"Odoo_Record__c", "Odoo_Sync_Error__c"}
INT = {"Odoo_ID__c", "Odoo_Line_ID__c"}
QTY = {"Odoo_Qty_Delivered__c", "Odoo_Qty_Invoiced__c"}
BOOL = {"Odoo_Fulfilled__c"}


def _chunks(items, size):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _soql(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _show(value):
    if value is None or value is False or value == "":
        return "-"
    return str(value)[:60]


def _norm(field, value):
    """Comparable form of one field's value. Blank and null compare equal."""
    if field in TEXT:
        return (value or "").replace("\r\n", "\n").strip()
    if field in INT:
        return None if value is None or value == "" else int(value)
    if field in QTY:
        return round(float(value or 0), 2)
    if field in BOOL:
        return bool(value)
    return value


def _wire(field, value):
    """Value as sent to Salesforce. Blank text goes as null, which clears the field."""
    if field in TEXT:
        return _norm(field, value) or None
    return _norm(field, value)


def _changes(current, desired):
    """Fields whose Salesforce value differs from the wanted one, as sent to Salesforce."""
    return {f: _wire(f, w) for f, w in desired.items() if _norm(f, current.get(f)) != _norm(f, w)}


def _error_text(messages):
    """Odoo_Sync_Error__c: this run's errors and warnings for the record, one per line. None when clean."""
    text = "\n".join(dict.fromkeys(messages))  # keeps the order, drops repeats
    if len(text) > ERROR_MAX:
        text = text[:ERROR_MAX] + "\n...(truncated)"
    return text or None


def _line_values(rec):
    """§5.1 quantities and fulfilment for one order line. Deliverable lines (professional services)
    use their deliverable count and are fulfilled when all deliverables are. Other lines
    (subscriptions) are fulfilled when fully invoiced."""
    invoiced = rec["qty_invoiced"] or 0
    if (rec["deliverable_count"] or 0) > 0:
        delivered = rec["deliverable_delivered_count"] or 0
        fulfilled = delivered >= rec["deliverable_count"]
    else:
        delivered = rec["qty_delivered"] or 0
        quantity = rec["product_uom_qty"] or 0
        fulfilled = quantity > 0 and round(invoiced, 2) >= round(quantity, 2)
    return {
        "Odoo_Line_ID__c": rec["id"],
        "Odoo_Qty_Delivered__c": delivered,
        "Odoo_Qty_Invoiced__c": invoiced,
        "Odoo_Fulfilled__c": fulfilled,
    }


class WriteBack:
    def __init__(self, sf, odoo, dry_run):
        self.sf = sf
        self.odoo = odoo
        self.dry_run = dry_run
        self.counts = Counter()              # (sobject, status) -> n
        self.failures = defaultdict(list)    # "<object>: <error>" -> [Salesforce Id]
        self.pending = defaultdict(list)     # sobject -> records waiting to be sent
        self.fields_ok = {}                  # sobject -> bool, checked once per run
        self.now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ----- gates -----

    def _ready(self):
        """True if this run may write here. Raises SalesforceError when a real write would be unsafe."""
        if not self.dry_run and not config.SF_WRITEBACK:
            print("Salesforce write-back off (SF_WRITEBACK is not true): nothing written to Salesforce")
            return False
        if not config.ODOO_PUBLIC_URL:
            raise SalesforceError("ODOO_PUBLIC_URL is not set (connector/.env)")
        org = self.sf.session.org_id or "unknown"
        matches = bool(config.SF_EXPECTED_ORG_ID) and org == config.SF_EXPECTED_ORG_ID
        if self.dry_run:
            print(f"Salesforce org {org}: {'matches' if matches else 'does not match'} SF_EXPECTED_ORG_ID")
            return True
        if not config.SF_EXPECTED_ORG_ID:
            raise SalesforceError("SF_EXPECTED_ORG_ID is not set; nothing written to Salesforce")
        if not matches:
            raise SalesforceError(f"authenticated org {org} is not SF_EXPECTED_ORG_ID; nothing written to Salesforce")
        return True

    def _fields_ok(self, sobject):
        """True if every field write-back needs exists on the object, with the right type, and is
        updateable. Field names are matched without regard to case, as Salesforce does. On a real run
        a failing object is skipped; a dry run still prints its planned changes, marked as stopped."""
        if sobject not in self.fields_ok:
            described = {f["name"].lower(): f for f in self.sf.describe(sobject)["fields"]}
            problems = []
            for name, sf_type in REQUIRED[sobject].items():
                field = described.get(name.lower())
                if field is None:
                    problems.append(f"{name} does not exist or is not visible to the integration user")
                elif field["type"] != sf_type:
                    problems.append(f"{name} is {field['type']}, expected {sf_type}")
                elif not field["updateable"]:
                    problems.append(f"{name} is not updateable by the integration user")
            verb = "would stop" if self.dry_run else "stopped"
            for problem in problems:
                print(f"Salesforce write-back for {sobject} {verb}: {problem}")
            self.fields_ok[sobject] = not problems or self.dry_run
        return self.fields_ok[sobject]

    # ----- reads -----

    def _current(self, sobject, ids):
        """Current Salesforce values of the given records: Id -> record."""
        fields = ", ".join(REQUIRED[sobject])
        rows = {}
        for chunk in _chunks(ids, BATCH):
            id_list = ", ".join(_soql(i) for i in chunk)
            for row in self.sf.query(f"SELECT Id, {fields} FROM {sobject} WHERE Id IN ({id_list})"):
                rows[row["Id"]] = row
        return rows

    def _odoo_rows(self, model, field, values, fields, extra_domain=()):
        rows = []
        for chunk in _chunks(values, 500):
            rows += self.odoo.search_read(model, [(field, "in", chunk), *extra_domain], fields)
        return rows

    # ----- planning and sending -----

    def _plan(self, sobject, sf_id, label, current, desired, stamp=False):
        """Queue the record if any wanted value differs. stamp: also set Odoo_Last_Sync__c, which is
        done only when something else on the record changes."""
        changes = _changes(current, desired)
        if not changes:
            self.counts[(sobject, "unchanged")] += 1
            return
        if stamp:
            changes["Odoo_Last_Sync__c"] = self.now
        detail = ", ".join(f"{f}: {_show(current.get(f))} -> {_show(v)}" for f, v in changes.items())
        prefix = "[dry-run] would update" if self.dry_run else "update"
        name = f" {label}" if label else ""
        print(f"{prefix} {sobject} {sf_id}{name}: {detail}")
        self.counts[(sobject, "planned")] += 1
        self.pending[sobject].append({"attributes": {"type": sobject}, "Id": sf_id, **changes})

    def _flush(self, sobject):
        records = self.pending.pop(sobject, [])
        if self.dry_run:
            return
        for chunk in _chunks(records, BATCH):
            try:
                results = self.sf.request(
                    "PATCH", f"/services/data/v{self.sf.api_version}/composite/sobjects",
                    {"allOrNone": False, "records": chunk},
                )
            except SalesforceError as err:
                results = [{"id": r["Id"], "success": False,
                            "errors": [{"statusCode": "REQUEST", "message": str(err)}]} for r in chunk]
            for result in results:
                if result.get("success"):
                    continue
                errors = result.get("errors") or [{"statusCode": "UNKNOWN", "message": "no detail returned"}]
                kind = f"{sobject}: " + "; ".join(f"{e.get('statusCode')} {e.get('message')}" for e in errors)
                self.failures[kind].append(result.get("id"))
                self.counts[(sobject, "failed")] += 1

    # ----- objects -----

    def accounts(self, records, issues):
        """Account: Odoo_ID__c and Odoo_Record__c (res.partner), Odoo_Sync_Error__c for this run."""
        if not self._ready() or not self._fields_ok("Account"):
            return
        records = list({r["Id"]: r for r in records}.values())
        ids = [r["Id"] for r in records]
        partners = {r["sf_account_id"]: r["id"] for r in self._odoo_rows(
            "res.partner", "sf_account_id", ids, ["id", "sf_account_id"], [("active", "in", [True, False])])}
        current = self._current("Account", ids)
        for rec in records:
            sf_id = rec["Id"]
            desired = {"Odoo_Sync_Error__c": _error_text(issues.get(sf_id, []))}
            if sf_id in partners:
                desired["Odoo_ID__c"] = partners[sf_id]
                desired["Odoo_Record__c"] = f"{config.ODOO_PUBLIC_URL}/odoo/contacts/{partners[sf_id]}"
            else:
                self.counts[("Account", "no Odoo record")] += 1
            self._plan("Account", sf_id, rec["Name"], current.get(sf_id, {}), desired, stamp=True)
        self._flush("Account")

    def opportunities(self, opps, issues, sf_lines):
        """Opportunity: Odoo_ID__c and Odoo_Record__c (sale.order), Odoo_Sync_Error__c for this run, and
        the lines of those opportunities (sf_lines: opportunity Id -> OpportunityLineItems)."""
        if not self._ready():
            return
        if self._fields_ok("Opportunity"):
            opps = list({o["Id"]: o for o in opps}.values())
            ids = [o["Id"] for o in opps]
            orders = {r["sf_opportunity_id"]: r["id"] for r in self._odoo_rows(
                "sale.order", "sf_opportunity_id", ids, ["id", "sf_opportunity_id"])}
            current = self._current("Opportunity", ids)
            for opp in opps:
                sf_id = opp["Id"]
                messages = list(issues.get(sf_id, []))
                order_id = orders.get(sf_id)
                if order_id and opp["StageName"] not in (config.STAGE_AWARD_WON, config.STAGE_CLOSED):
                    messages.append(f"stage moved out of {config.STAGE_AWARD_WON}, order not changed, "
                                    f"review in Odoo: Salesforce stage is {opp['StageName']}")
                desired = {"Odoo_Sync_Error__c": _error_text(messages)}
                if order_id:
                    desired["Odoo_ID__c"] = order_id
                    desired["Odoo_Record__c"] = f"{config.ODOO_PUBLIC_URL}/odoo/action-sale.action_orders/{order_id}"
                else:
                    self.counts[("Opportunity", "no Odoo record")] += 1
                self._plan("Opportunity", sf_id, opp["Name"], current.get(sf_id, {}), desired, stamp=True)
            self._flush("Opportunity")
        self._lines(sf_lines)

    def _lines(self, sf_lines):
        """OpportunityLineItem: Odoo_Line_ID__c, Odoo_Qty_Delivered__c, Odoo_Qty_Invoiced__c, Odoo_Fulfilled__c."""
        lines = [line for group in sf_lines.values() for line in group]
        if not lines or not self._fields_ok("OpportunityLineItem"):
            return
        ids = [line["Id"] for line in lines]
        odoo_lines = {r["sf_line_id"]: r for r in self._odoo_rows(
            "sale.order.line", "sf_line_id", ids, ODOO_LINE_FIELDS)}
        current = self._current("OpportunityLineItem", ids)
        for line in lines:
            rec = odoo_lines.get(line["Id"])
            if rec is None:
                self.counts[("OpportunityLineItem", "no Odoo record")] += 1
                continue
            self._plan("OpportunityLineItem", line["Id"], "", current.get(line["Id"], {}), _line_values(rec))
        self._flush("OpportunityLineItem")

    # ----- output -----

    def summary(self):
        c = self.counts
        if not c:
            return  # write-back was off, or nothing was checked
        mode = "dry run, nothing written" if self.dry_run else "written to Salesforce"
        verb = "would update" if self.dry_run else "updated"
        parts = []
        for sobject, label in (("Account", "accounts"), ("Opportunity", "opportunities"),
                               ("OpportunityLineItem", "lines")):
            planned, failed = c[(sobject, "planned")], c[(sobject, "failed")]
            parts.append(f"{label} {verb} {planned - failed}, unchanged {c[(sobject, 'unchanged')]}, "
                         f"failed {failed}, no Odoo record {c[(sobject, 'no Odoo record')]}")
        print(f"\nSalesforce write-back ({mode}): " + "; ".join(parts))
        if self.failures:
            total = sum(len(ids) for ids in self.failures.values())
            print(f"Salesforce failures ({total}), grouped by type:")
            for kind, ids in sorted(self.failures.items(), key=lambda kv: -len(kv[1])):
                print(f"  {len(ids):>5}  {kind}")
                print(f"           e.g. {ids[0]}")


def write_accounts(sf, odoo, dry_run, records, issues):
    """Account write-back for the records a sync run handled. Does not raise: the Odoo side is already done."""
    wb = WriteBack(sf, odoo, dry_run)
    try:
        wb.accounts(records, issues)
    except (SalesforceError, OdooError) as err:
        print(f"Salesforce write-back stopped: {err}")
    wb.summary()


def write_opportunities(sf, odoo, dry_run, opps, issues, sf_lines):
    """Opportunity and line write-back for a sync run. Does not raise: the Odoo side is already done."""
    wb = WriteBack(sf, odoo, dry_run)
    try:
        wb.opportunities(opps, issues, sf_lines)
    except (SalesforceError, OdooError) as err:
        print(f"Salesforce write-back stopped: {err}")
    wb.summary()
