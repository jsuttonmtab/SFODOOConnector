"""Odoo -> Salesforce write-back (phase 1): Odoo ids and links, sync errors, and line quantities.

Runs after the Odoo side of sync_accounts and sync_orders. For the records a run handled, it reads the
Odoo state, compares it with what Salesforce already holds, and writes only the fields that differ,
200 records per call (Composite sObject Collections). A failed write is reported and does not stop the
Odoo sync. Rules: docs/field-mapping.md §5.1.

Writes happen only when SF_WRITEBACK=true, and only after the org check in sf_client.check_org passes.
--dry-run prints the field changes per record and writes nothing; it does not need SF_WRITEBACK.
"""
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import config
from .odoo_client import OdooError, m2o_id
from .sf_client import SalesforceError, check_org

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
    """Value for the dry-run and log lines. Shown in full (a message is what is written to Salesforce);
    line breaks become ' | ' so each record stays on one line."""
    if value is None or value is False or value == "":
        return "-"
    return str(value).replace("\n", " | ")


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


def _visible(messages, has_order=True):
    """The messages Sales can act on in Salesforce. The classification is in config: SF_VISIBLE_ISSUES
    always, SF_VISIBLE_IF_ORDERED only when the record has a synced order (has_order)."""
    shown = []
    for m in messages:
        if any(p in m for p in config.SF_VISIBLE_ISSUES):
            shown.append(m)
        elif has_order and any(p in m for p in config.SF_VISIBLE_IF_ORDERED):
            shown.append(m)
    return shown


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


# Opportunity Ids named in a FIELD_INTEGRITY_EXCEPTION message: 15 or 18 characters, key prefix 006.
OPP_ID = re.compile(r"\b006[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?\b")
INTEGRITY = "FIELD_INTEGRITY_EXCEPTION"
MESSAGE_SHOWN = 300  # characters of an error message shown in the run summary


def _field_map(sf, sobject):
    """The object's fields from describe, keyed by lower-case API name. Read once per run: the cache sits on the
    Salesforce session, so the accounts and the opportunity write-backs share one describe per object."""
    cache = sf.session.__dict__.setdefault("field_maps", {})
    if sobject not in cache:
        cache[sobject] = {f["name"].lower(): f for f in sf.describe(sobject)["fields"]}
    return cache[sobject]


def _opportunity_ids(messages):
    """Opportunity Ids named in the error messages, each once, in order of appearance."""
    found = []
    for message in messages:
        found += OPP_ID.findall(message)
    return list(dict.fromkeys(found))


class WriteBack:
    def __init__(self, sf, odoo, dry_run):
        self.sf = sf
        self.odoo = odoo
        self.dry_run = dry_run
        self.counts = Counter()              # (sobject, status) -> n
        self.failed = []                     # one entry per record Salesforce did not write
        self.labels = {}                     # (sobject, Id) -> name, for the summary
        self.pending = defaultdict(list)     # sobject -> [(group key, payload)] waiting to be sent
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
        check_org(self.sf, self.dry_run, log=False)  # the run has already logged the org
        return True

    def _fields_ok(self, sobject):
        """True if every field write-back needs exists on the object, with the right type, and is
        updateable. Names are matched without regard to case, as Salesforce does. The org's own API name of
        each field is taken from describe (see _api). A case difference is announced once per run. A failing
        object is skipped in a real run and in a dry run alike: its current values cannot be read without the
        fields, so a dry run reports what a real run would stop on, and nothing more."""
        if sobject not in self.fields_ok:
            described = _field_map(self.sf, sobject)
            announced = self.sf.session.__dict__.setdefault("announced_case", set())
            problems = []
            for name, sf_type in REQUIRED[sobject].items():
                field = described.get(name.lower())
                if field is None:
                    problems.append(f"{name} does not exist or is not visible to the integration user")
                elif field["type"] != sf_type:
                    problems.append(f"{name} is {field['type']}, expected {sf_type}")
                elif not field["updateable"]:
                    problems.append(f"{name} is not updateable by the integration user")
                elif field["name"] != name and (sobject, name) not in announced:
                    announced.add((sobject, name))
                    print(f"Salesforce field name for {sobject}: {name} is {field['name']} in this org (case differs)")
            verb = "would stop" if self.dry_run else "stopped"
            for problem in problems:
                print(f"Salesforce write-back for {sobject} {verb}: {problem}")
            self.fields_ok[sobject] = not problems
        return self.fields_ok[sobject]

    def _api(self, sobject, name):
        """The org's API name of a configured field, as describe returns it. Lookup ignores case."""
        return _field_map(self.sf, sobject)[name.lower()]["name"]

    # ----- reads -----

    def _current(self, sobject, ids):
        """Current Salesforce values of the given records: Id -> {configured field name: value}. The SOQL and
        the results use the org's API names, so a difference in case from the configured name cannot make a
        record look blank. The result is keyed by the configured names, as _changes expects."""
        actual = {field: self._api(sobject, field) for field in REQUIRED[sobject]}
        fields = ", ".join(actual.values())
        rows = {}
        for chunk in _chunks(ids, BATCH):
            id_list = ", ".join(_soql(i) for i in chunk)
            for row in self.sf.query(f"SELECT Id, {fields} FROM {sobject} WHERE Id IN ({id_list})"):
                rows[row["Id"]] = {"Id": row["Id"], **{field: row.get(name) for field, name in actual.items()}}
        return rows

    def _odoo_rows(self, model, field, values, fields, extra_domain=()):
        rows = []
        for chunk in _chunks(values, 500):
            rows += self.odoo.search_read(model, [(field, "in", chunk), *extra_domain], fields)
        return rows

    # ----- planning and sending -----

    def _plan(self, sobject, sf_id, label, current, desired, stamp=False, count_unchanged=True, group=None):
        """Queue the record if any wanted value differs. stamp: also set Odoo_Last_Sync__c, which is
        done only when something else on the record changes. count_unchanged False: a record with no
        Odoo record yet is counted under "no Odoo record", not as unchanged. group: records with the same
        group are sent in the same Salesforce call (the lines of one opportunity). The PATCH body uses the
        org's API names."""
        self.labels[(sobject, sf_id)] = label
        changes = _changes(current, desired)
        if not changes:
            if count_unchanged:
                self.counts[(sobject, "unchanged")] += 1
            return
        if stamp:
            changes["Odoo_Last_Sync__c"] = self.now
        detail = ", ".join(f"{f}: {_show(current.get(f))} -> {_show(v)}" for f, v in changes.items())
        prefix = "[dry-run] would update" if self.dry_run else "update"
        name = f" {label}" if label else ""
        print(f"{prefix} {sobject} {sf_id}{name}: {detail}")
        self.counts[(sobject, "planned")] += 1
        payload = {"attributes": {"type": sobject}, "Id": sf_id,
                   **{self._api(sobject, field): value for field, value in changes.items()}}
        self.pending[sobject].append((sf_id if group is None else group, payload))

    def _flush(self, sobject):
        """Send the planned records of one object, BATCH records per call. Records of one group are never
        split between calls; a group larger than BATCH is sent on its own."""
        entries = self.pending.pop(sobject, [])
        if self.dry_run:
            return
        units = {}  # group key -> payloads, in order of first appearance
        for key, payload in entries:
            units.setdefault(key, []).append(payload)
        batch, size = [], 0
        for unit in units.values():
            if batch and size + len(unit) > BATCH:
                self._send(sobject, batch)
                batch, size = [], 0
            batch.append(unit)
            size += len(unit)
        if batch:
            self._send(sobject, batch)

    def _send(self, sobject, units):
        """Write the records of these units (lists of payloads) in one Salesforce call, with allOrNone
        false. When the call fails as a whole, or every record in it fails, the units are split in halves
        and each half is sent again, recursively, until a failing record is alone (a unit is never split).
        Records that succeed are kept. A record that fails on its own is reported in self.failed."""
        records = [payload for unit in units for payload in unit]
        try:
            results = self.sf.request(
                "PATCH", f"/services/data/v{self.sf.api_version}/composite/sobjects",
                {"allOrNone": False, "records": records},
            )
            if not isinstance(results, list) or len(results) != len(records):
                raise SalesforceError("Salesforce returned an unexpected response to a write")
            # Results come back in the order sent. A failed result has no Id, so results are matched by position.
            errors = [None if r.get("success") else
                      (r.get("errors") or [{"statusCode": "UNKNOWN", "message": "no detail returned"}])
                      for r in results]
            whole = None
        except SalesforceError as err:
            errors = None
            whole = [{"statusCode": "REQUEST", "message": str(err)}]
        failed_all = errors is None or all(e is not None for e in errors)
        if failed_all and len(units) > 1:
            half = len(units) // 2
            self._send(sobject, units[:half])
            self._send(sobject, units[half:])
            return
        if errors is None:
            errors = [whole] * len(records)
        for payload, error in zip(records, errors):
            self._outcome(sobject, payload["Id"], error)

    def _outcome(self, sobject, sf_id, error):
        """Count one record that Salesforce did not write. error None: the record was written."""
        if error is None:
            return
        self.counts[(sobject, "failed")] += 1
        self.failed.append({
            "sobject": sobject,
            "id": sf_id,
            "name": self.labels.get((sobject, sf_id), ""),
            "code": " + ".join(sorted({e.get("statusCode") or "UNKNOWN" for e in error})),
            "messages": [e.get("message") or "" for e in error],
        })

    # ----- objects -----

    def _error_counts(self, sobject, messages, has_order=True):
        """Tally one record for the dry-run report: whether its Odoo_Sync_Error__c would be non-blank
        with every message (before) and with the Sales-visible ones only (after)."""
        self.counts[(sobject, "error checked")] += 1
        if messages:
            self.counts[(sobject, "error before")] += 1
        if _visible(messages, has_order):
            self.counts[(sobject, "error after")] += 1

    def accounts(self, records, issues):
        """Account: Odoo_ID__c and Odoo_Record__c (res.partner), Odoo_Sync_Error__c for this run."""
        if not self._ready() or not self._fields_ok("Account"):
            return
        records = list({r["Id"]: r for r in records}.values())
        ids = [r["Id"] for r in records]
        partners = {r["sf_account_id"]: r["id"] for r in self._odoo_rows(
            "res.partner", "sf_account_id", ids, ["id", "sf_account_id"], [("active", "in", [True, False])])}
        # Partners with a synced opportunity: a sale order carrying an sf_opportunity_id.
        ordered = {m2o_id(r["partner_id"]) for r in self._odoo_rows(
            "sale.order", "partner_id", list(partners.values()), ["partner_id"],
            [("sf_opportunity_id", "!=", False)])}
        current = self._current("Account", ids)
        for rec in records:
            sf_id = rec["Id"]
            messages = issues.get(sf_id, [])
            has_order = partners.get(sf_id) in ordered
            self._error_counts("Account", messages, has_order)
            desired = {"Odoo_Sync_Error__c": _error_text(_visible(messages, has_order))}
            if sf_id in partners:
                desired["Odoo_ID__c"] = partners[sf_id]
                desired["Odoo_Record__c"] = f"{config.ODOO_PUBLIC_URL}/odoo/contacts/{partners[sf_id]}"
            else:
                self.counts[("Account", "no Odoo record")] += 1
            self._plan("Account", sf_id, rec["Name"], current.get(sf_id, {}), desired, stamp=True,
                       count_unchanged=sf_id in partners)
        self._flush("Account")

    def opportunities(self, opps, issues, sf_lines):
        """Opportunity: Odoo_ID__c and Odoo_Record__c (sale.order), Odoo_Sync_Error__c for this run, and
        the lines of those opportunities (sf_lines: opportunity Id -> OpportunityLineItems)."""
        if not self._ready():
            return
        if self._fields_ok("Opportunity"):
            opps = list({o["Id"]: o for o in opps}.values())
            ids = [o["Id"] for o in opps]
            rows = self._odoo_rows("sale.order", "sf_opportunity_id", ids,
                                   ["id", "sf_opportunity_id", "sf_cancelled_by_sync"])
            orders = {r["sf_opportunity_id"]: r["id"] for r in rows}
            cancelled = {r["sf_opportunity_id"] for r in rows if r.get("sf_cancelled_by_sync")}
            current = self._current("Opportunity", ids)
            for opp in opps:
                sf_id = opp["Id"]
                messages = list(issues.get(sf_id, []))
                order_id = orders.get(sf_id)
                if order_id and opp["StageName"] not in (config.STAGE_AWARD_WON, config.STAGE_CLOSED):
                    if sf_id in cancelled:
                        # The connector cancelled the quotation. The message goes when it is reopened.
                        messages.append(f"quotation cancelled in Odoo because the opportunity left "
                                        f"{config.STAGE_AWARD_WON} (Salesforce stage is {opp['StageName']}); "
                                        f"it is reopened if the opportunity returns")
                    else:
                        messages.append(f"stage moved out of {config.STAGE_AWARD_WON}, order not changed, "
                                        f"review in Odoo: Salesforce stage is {opp['StageName']}")
                self._error_counts("Opportunity", messages)
                desired = {"Odoo_Sync_Error__c": _error_text(_visible(messages))}
                if order_id:
                    desired["Odoo_ID__c"] = order_id
                    desired["Odoo_Record__c"] = f"{config.ODOO_PUBLIC_URL}/odoo/action-sale.action_orders/{order_id}"
                else:
                    self.counts[("Opportunity", "no Odoo record")] += 1
                self._plan("Opportunity", sf_id, opp["Name"], current.get(sf_id, {}), desired, stamp=True)
            self._flush("Opportunity")
        self._lines(sf_lines)

    def _lines(self, sf_lines):
        """OpportunityLineItem: Odoo_Line_ID__c, Odoo_Qty_Delivered__c, Odoo_Qty_Invoiced__c, Odoo_Fulfilled__c.
        The lines of one opportunity are one group: they are sent in the same call."""
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
            product = line.get("Product2") or {}
            self._plan("OpportunityLineItem", line["Id"], product.get("Name") or "",
                       current.get(line["Id"], {}), _line_values(rec), group=line["OpportunityId"])
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
        for sobject, label in (("Account", "accounts"), ("Opportunity", "opportunities")):
            if c[(sobject, "error checked")]:
                print(f"Odoo_Sync_Error__c non-blank, {label}: {c[(sobject, 'error before')]} with every message, "
                      f"{c[(sobject, 'error after')]} with Sales-visible messages only "
                      f"(of {c[(sobject, 'error checked')]} checked)")
        if self.failed:
            self._report_failed()

    def _report_failed(self):
        """Records Salesforce did not write, grouped by error code, each with its Id and name. The
        FIELD_INTEGRITY_EXCEPTION group is explained once, with the opportunities named in its messages."""
        print(f"Salesforce records NOT written ({len(self.failed)}), grouped by error code:")
        groups = defaultdict(list)
        for row in self.failed:
            groups[row["code"]].append(row)
        for code, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            print(f"  {code}: {len(rows)} record(s)")
            if code == INTEGRITY:
                opp_ids = _opportunity_ids(m for row in rows for m in row["messages"])
                print("    opportunity totals don't match its products in Salesforce (data problem in this org)")
                print(f"    opportunities ({len(opp_ids)}):")
                for opp_id in opp_ids:
                    name = self.labels.get(("Opportunity", opp_id)) or "name not in this run"
                    print(f"      {opp_id}  {name}")
                first = next((m for row in rows for m in row["messages"] if m), "")
                print(f"    message: {first[:MESSAGE_SHOWN]}")
            for row in rows:
                line = f"    {row['sobject']} {row['id']}  {row['name']}"
                if code != INTEGRITY:
                    line += "  " + "; ".join(m for m in row["messages"] if m)[:MESSAGE_SHOWN]
                print(line.rstrip())


def write_accounts(sf, odoo, dry_run, records, issues):
    """Account write-back for the records a sync run handled. Does not raise: the Odoo side is already done.
    Returns the number of records Salesforce did not write."""
    wb = WriteBack(sf, odoo, dry_run)
    try:
        wb.accounts(records, issues)
    except (SalesforceError, OdooError) as err:
        print(f"Salesforce write-back stopped: {err}")
    wb.summary()
    return len(wb.failed)


def write_opportunities(sf, odoo, dry_run, opps, issues, sf_lines):
    """Opportunity and line write-back for a sync run. Does not raise: the Odoo side is already done.
    Returns the number of records Salesforce did not write."""
    wb = WriteBack(sf, odoo, dry_run)
    try:
        wb.opportunities(opps, issues, sf_lines)
    except (SalesforceError, OdooError) as err:
        print(f"Salesforce write-back stopped: {err}")
    wb.summary()
    return len(wb.failed)
