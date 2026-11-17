"""Odoo -> Salesforce: posted customer invoice PDFs on the opportunity (write-back phase 2, docs/field-mapping.md §5.2).

Each posted customer invoice linked to a synced order is attached to that order's opportunity as a classic
Attachment named "<invoice number>.pdf" ("/" replaced by "-"), the mechanism and naming Celigo used. An invoice
that spans two orders is attached to each opportunity once. A credit note (out_refund) follows
SF_INVOICE_PDF_REFUNDS.

Idempotent. An invoice whose PDF was sent since its last change is skipped without rendering. A rendered PDF whose
checksum and size match the attachment already on the opportunity is unchanged. A changed PDF replaces the
attachment of the same name (PATCH on Attachment.Body); it never adds a second one.

Gates: SF_WRITEBACK and SF_INVOICE_PDF, plus the sandbox guard (sf_client.check_org). --dry-run prints what would be
attached and writes nothing. Does not write OpportunityLineItem. Does not raise: the Odoo side is already done.
"""
import base64
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import config
from .odoo_client import OdooError, m2o_id
from .sf_client import SalesforceError, check_org

INVOICE_TYPES = ("out_invoice", "out_refund")
MOVE_FIELDS = ["id", "name", "move_type", "state", "write_date", "sf_pdf_checksum", "sf_pdf_sent_at"]
ID_BATCH = 500
OPP_BATCH = 150
# An invoice changed after it was sent is one whose write_date is later than sf_pdf_sent_at. Recording the send
# is itself a write, so write_date may be a few seconds after sf_pdf_sent_at. Allow this much before it counts.
SENT_TOLERANCE = timedelta(seconds=60)
STAMP = "%Y-%m-%d %H:%M:%S"


def attachment_name(invoice_name):
    """The Salesforce attachment name for an invoice: "<invoice number>.pdf", "/" replaced by "-"."""
    return f"{(invoice_name or '').replace('/', '-')}.pdf"


def _batches(items, size):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _quote(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _parse(stamp):
    return datetime.strptime(stamp, STAMP).replace(tzinfo=timezone.utc) if stamp else None


def _sent_since_change(move):
    """True when the PDF was recorded after the invoice last changed (so it need not be rendered again)."""
    sent, changed = _parse(move.get("sf_pdf_sent_at")), _parse(move.get("write_date"))
    return bool(sent and changed and changed <= sent + SENT_TOLERANCE)


def invoice_targets(odoo, opportunity=None, refunds=True):
    """Posted customer invoices linked to synced orders: {invoice id: {"move": row, "opps": {opportunity Ids}}}.
    The link is invoice line -> sale line -> order -> sf_opportunity_id. An invoice on two orders maps to two
    opportunities. opportunity: only that opportunity. refunds False: credit notes are left out."""
    orders = odoo.search_read("sale.order", [("sf_opportunity_id", "!=", False)], ["id", "sf_opportunity_id"])
    order_opp = {o["id"]: o["sf_opportunity_id"] for o in orders
                 if not opportunity or o["sf_opportunity_id"] == opportunity}
    line_opp = {}
    for chunk in _batches(list(order_opp), ID_BATCH):
        for line in odoo.search_read("sale.order.line", [("order_id", "in", chunk)], ["id", "order_id"]):
            line_opp[line["id"]] = order_opp[m2o_id(line["order_id"])]
    linked = {}
    for chunk in _batches(list(line_opp), ID_BATCH):
        for inv_line in odoo.search_read("account.move.line", [("sale_line_ids", "in", chunk)],
                                         ["move_id", "sale_line_ids"]):
            move_id = m2o_id(inv_line["move_id"])
            for sale_line in inv_line["sale_line_ids"] or []:
                if sale_line in line_opp:
                    linked.setdefault(move_id, set()).add(line_opp[sale_line])
    types = list(INVOICE_TYPES) if refunds else ["out_invoice"]
    targets = {}
    for chunk in _batches(list(linked), ID_BATCH):
        for move in odoo.search_read("account.move", [("id", "in", chunk), ("state", "=", "posted"),
                                                      ("move_type", "in", types)], MOVE_FIELDS):
            targets[move["id"]] = {"move": move, "opps": linked[move["id"]]}
    return targets


def existing_attachments(sf, opp_ids):
    """Attachments already on the opportunities: (opportunity Id, Name) -> [Attachment rows]."""
    found = {}
    for chunk in _batches(sorted(opp_ids), OPP_BATCH):
        ids = ", ".join(_quote(i) for i in chunk)
        for row in sf.query(f"SELECT Id, ParentId, Name, BodyLength FROM Attachment WHERE ParentId IN ({ids})"):
            found.setdefault((row["ParentId"], row["Name"]), []).append(row)
    return found


def _now():
    return datetime.now(timezone.utc).strftime(STAMP)


def write_invoice_pdfs(sf, odoo, dry_run, opportunity=None):
    """Attach the posted customer invoices of synced orders to their opportunities. Prints one line per
    invoice and opportunity and a summary. Returns the number of records that failed (0 if the step was off)."""
    if not config.SF_INVOICE_PDF:
        print("Invoice PDFs off (SF_INVOICE_PDF is not true): nothing attached")
        return 0
    if not dry_run and not config.SF_WRITEBACK:
        print("Invoice PDFs off (SF_WRITEBACK is not true): nothing attached")
        return 0
    counts = Counter()
    failed = []
    try:
        check_org(sf, dry_run, log=False)
        targets = invoice_targets(odoo, opportunity, config.SF_INVOICE_PDF_REFUNDS)
        attachments = existing_attachments(sf, {opp for t in targets.values() for opp in t["opps"]})
    except (SalesforceError, OdooError) as err:
        print(f"Invoice PDFs stopped: {err}")
        return 0

    prefix = "[dry-run] would " if dry_run else ""
    for move_id in sorted(targets):
        move, opps = targets[move_id]["move"], sorted(targets[move_id]["opps"])
        name = attachment_name(move["name"])
        if all(attachments.get((opp, name)) for opp in opps) and _sent_since_change(move):
            counts["unchanged"] += len(opps)
            for opp in opps:
                print(f"unchanged {name} on {opp} (sent since the invoice last changed)")
            continue
        try:
            rendered = odoo.call("account.move", "sf_invoice_pdf", ids=[move_id])[0]
        except OdooError as err:
            for opp in opps:
                counts["failed"] += 1
                failed.append((opp, move["name"], f"could not render the invoice PDF: {err}"))
            continue
        body, checksum = rendered["pdf_base64"], rendered["checksum"]
        size = len(base64.b64decode(body))
        move_ok = True
        for opp in opps:
            rows = attachments.get((opp, name), [])
            if not rows:
                action = "attach"
            elif move.get("sf_pdf_checksum") == checksum and rows[0]["BodyLength"] == size:
                action = "unchanged"
            else:
                action = "replace"
            if action == "unchanged":
                counts["unchanged"] += 1
                print(f"unchanged {name} on {opp}")
                continue
            if len(rows) > 1:
                print(f"WARNING {name} is on {opp} {len(rows)} times: the first is replaced, the others are left")
            print(f"{prefix}{'attach' if action == 'attach' else 'replace'} {name} on {opp} ({size} bytes)")
            if dry_run:
                counts["attached" if action == "attach" else "replaced"] += 1
                continue
            try:
                if action == "attach":
                    sf.request("POST", f"/services/data/v{sf.api_version}/sobjects/Attachment",
                               {"ParentId": opp, "Name": name, "Body": body})
                    counts["attached"] += 1
                else:
                    sf.request("PATCH", f"/services/data/v{sf.api_version}/sobjects/Attachment/{rows[0]['Id']}",
                               {"Body": body})
                    counts["replaced"] += 1
            except SalesforceError as err:
                move_ok = False
                counts["failed"] += 1
                failed.append((opp, move["name"], f"Salesforce: {err}"))
        if not dry_run and move_ok:  # record the send, so the next run can skip this invoice without rendering
            try:
                odoo.write("account.move", [move_id], {"sf_pdf_checksum": checksum, "sf_pdf_sent_at": _now()})
            except OdooError as err:
                failed.append(("-", move["name"], f"could not record the send on the invoice: {err}"))
                counts["failed"] += 1

    mode = "dry run, nothing written" if dry_run else "written to Salesforce"
    print(f"\nInvoice PDFs ({mode}): attached {counts['attached']}, replaced {counts['replaced']}, "
          f"unchanged {counts['unchanged']}, failed {counts['failed']}")
    for opp, invoice, message in failed:
        print(f"  FAILED {invoice} on {opp}: {message}")
    return counts["failed"]
