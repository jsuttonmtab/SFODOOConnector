"""Invoice PDFs on the opportunity (sf_invoices.py). Fakes only: no Odoo or Salesforce calls.

Run from the repo root:  python -m unittest connector.test_invoice_pdf
"""
import base64
import hashlib
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from connector import config
from connector.sf_invoices import attachment_name, invoice_targets, write_invoice_pdfs
from connector.test_finance_review import FakeOdoo

OPP_A = "006AAAAAAAAAAAAAAA"
OPP_B = "006BBBBBBBBBBBBBBB"
ORG = "00Dhu000000FxMMEA0"
OLD = "2026-11-10 10:00:00"
NEW = "2026-11-16 09:00:00"  # the invoice changed after it was sent


RENDER_KEY = "render-key-1"  # what the fake Odoo reports as the invoice's render key by default


def pdf_bytes(tag):
    return f"%PDF-1.4 fake invoice {tag}".encode() * 40


class FakeInvoiceOdoo(FakeOdoo):
    """FakeOdoo that also answers the connector's JSON-2 method sf_invoice_pdf, and joins invoice lines to sale lines."""

    def __init__(self, tables, pdfs):
        super().__init__(tables)
        self.pdfs = pdfs  # invoice id -> bytes
        self.renders = []
        self.render_keys = {}  # invoice id -> render key (default RENDER_KEY)

    def call(self, model, method, **kwargs):
        if model == "account.move" and method == "sf_render_keys":
            # Odoo returns JSON, so the invoice ids are text.
            return {str(i): self.render_keys.get(i, RENDER_KEY) for i in kwargs["ids"]}
        if model == "account.move" and method == "sf_invoice_pdf":
            out = []
            for move_id in kwargs["ids"]:
                self.renders.append(move_id)
                pdf = self.pdfs[move_id]
                row = next(r for r in self.tables["account.move"] if r["id"] == move_id)
                name = row["name"]
                out.append({"id": move_id, "name": name, "filename": f"{name.replace('/', '-')}.pdf",
                            "pdf_base64": base64.b64encode(pdf).decode(), "checksum": hashlib.sha256(pdf).hexdigest()})
            return out
        return super().call(model, method, **kwargs)

    def search_read(self, model, domain, fields, **kwargs):
        if model == "account.move.line":
            wanted = next(v for f, op, v in domain if f == "sale_line_ids")
            return [dict(r) for r in self.tables.get(model, []) if set(r["sale_line_ids"]) & set(wanted)]
        return super().search_read(model, domain, fields, **kwargs)


class FakeOrgSF:
    """Salesforce: the sandbox check, the attachments query, and Attachment create and update."""
    api_version = "68.0"

    def __init__(self, attachments=()):
        self.session = SimpleNamespace()
        self.attachments = [dict(a) for a in attachments]
        self.writes = []  # (method, path, body)
        self._n = 0

    def query(self, soql):
        if "FROM Organization" in soql:
            return [{"Id": ORG, "IsSandbox": True, "Name": "sandbox"}]
        if "FROM Attachment" in soql:
            ids = re.findall(r"'([^']+)'", soql)
            return [dict(a) for a in self.attachments if a["ParentId"] in ids]
        raise AssertionError(soql)

    def request(self, method, path, body=None):
        self.writes.append((method, path, body))
        if method == "POST" and path.endswith("/sobjects/Attachment"):
            self._n += 1
            att = {"Id": f"00P{self._n:012d}", "ParentId": body["ParentId"], "Name": body["Name"],
                   "BodyLength": len(base64.b64decode(body["Body"]))}
            self.attachments.append(att)
            return {"id": att["Id"], "success": True}
        if method == "PATCH" and "/sobjects/Attachment/" in path:
            att_id = path.rsplit("/", 1)[1]
            for att in self.attachments:
                if att["Id"] == att_id:
                    att["BodyLength"] = len(base64.b64decode(body["Body"]))
            return None
        raise AssertionError((method, path))

    def sent(self, method):
        return [w for w in self.writes if w[0] == method]


def tables(moves, sale_orders, lines=None, inv_lines=None):
    return {
        "sale.order": sale_orders,
        "sale.order.line": lines or [],
        "account.move.line": inv_lines or [],
        "account.move": moves,
    }


def move(move_id, name, write_date=NEW, sent_at=None, checksum=None, move_type="out_invoice", render_key=RENDER_KEY):
    return {"id": move_id, "name": name, "move_type": move_type, "state": "posted", "write_date": write_date,
            "sf_pdf_checksum": checksum, "sf_pdf_sent_at": sent_at, "sf_pdf_render_key": render_key}


def order(order_id, opp):
    return {"id": order_id, "sf_opportunity_id": opp}


def line(line_id, order_id):
    return {"id": line_id, "order_id": order_id}


def inv_line(inv_id, move_id, sale_lines):
    return {"id": inv_id, "move_id": move_id, "sale_line_ids": sale_lines}


PDF = pdf_bytes("S00478")
NAME = attachment_name("INV/2026/00478")  # INV-2026-00478.pdf


class InvoicePdfTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(config, "SF_WRITEBACK", True),
            mock.patch.object(config, "SF_INVOICE_PDF", True),
            mock.patch.object(config, "SF_INVOICE_PDF_REFUNDS", True),
            mock.patch.object(config, "SF_EXPECTED_ORG_ID", ORG),
            mock.patch.object(config, "SF_REQUIRE_SANDBOX", True),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def one_order_tables(self, move_row):
        return tables([move_row], [order(10, OPP_A)], [line(100, 10)], [inv_line(1000, move_row["id"], [100])])

    def test_new_attach_names_the_file_and_records_the_send(self):
        odoo = FakeInvoiceOdoo(self.one_order_tables(move(500, "INV/2026/00478")), {500: PDF})
        sf = FakeOrgSF()
        failed = write_invoice_pdfs(sf, odoo, dry_run=False)
        self.assertEqual(failed, 0)
        posts = sf.sent("POST")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["ParentId"], OPP_A)
        self.assertEqual(posts[0][2]["Name"], NAME)
        self.assertEqual(base64.b64decode(posts[0][2]["Body"]), PDF)
        self.assertEqual(posts[0][1], "/services/data/v68.0/sobjects/Attachment")
        row = odoo.tables["account.move"][0]
        self.assertEqual(row["sf_pdf_checksum"], hashlib.sha256(PDF).hexdigest())
        self.assertTrue(row["sf_pdf_sent_at"])

    def test_unchanged_is_skipped_without_rendering(self):
        sent_at = NEW  # sent after the invoice last changed: the fast path
        m = move(500, "INV/2026/00478", write_date=OLD, sent_at=sent_at, checksum=hashlib.sha256(PDF).hexdigest())
        odoo = FakeInvoiceOdoo(self.one_order_tables(m), {500: PDF})
        sf = FakeOrgSF([{"Id": "00Pa", "ParentId": OPP_A, "Name": NAME, "BodyLength": len(PDF)}])
        self.assertEqual(write_invoice_pdfs(sf, odoo, dry_run=False), 0)
        self.assertEqual(odoo.renders, [])  # not rendered
        self.assertEqual(sf.writes, [])

    def test_changed_render_key_renders_again_even_when_sent_after_the_last_change(self):
        # A template or company-settings change: the invoice itself is unchanged and was sent after its last change,
        # but the render key differs from the one recorded with the send, so the PDF is rendered again.
        m = move(500, "INV/2026/00478", write_date=OLD, sent_at=NEW, checksum=hashlib.sha256(PDF).hexdigest(),
                 render_key="render-key-0")
        odoo = FakeInvoiceOdoo(self.one_order_tables(m), {500: PDF})
        sf = FakeOrgSF([{"Id": "00Pa", "ParentId": OPP_A, "Name": NAME, "BodyLength": len(PDF)}])
        write_invoice_pdfs(sf, odoo, dry_run=False)
        self.assertEqual(odoo.renders, [500])
        self.assertEqual(odoo.tables["account.move"][0]["sf_pdf_render_key"], RENDER_KEY)

    def test_send_records_the_render_key(self):
        odoo = FakeInvoiceOdoo(self.one_order_tables(move(500, "INV/2026/00478")), {500: PDF})
        write_invoice_pdfs(FakeOrgSF(), odoo, dry_run=False)
        self.assertEqual(odoo.tables["account.move"][0]["sf_pdf_render_key"], RENDER_KEY)

    def test_unchanged_after_render_writes_nothing_on_salesforce(self):
        # Changed since sent (so it is rendered), but the PDF is the same as the attachment: unchanged.
        m = move(500, "INV/2026/00478", write_date=NEW, sent_at=OLD, checksum=hashlib.sha256(PDF).hexdigest())
        odoo = FakeInvoiceOdoo(self.one_order_tables(m), {500: PDF})
        sf = FakeOrgSF([{"Id": "00Pa", "ParentId": OPP_A, "Name": NAME, "BodyLength": len(PDF)}])
        write_invoice_pdfs(sf, odoo, dry_run=False)
        self.assertEqual(odoo.renders, [500])
        self.assertEqual(sf.writes, [])

    def test_changed_invoice_replaces_the_same_attachment(self):
        # Reset to draft and posted again: the PDF changed. The attachment of the same name is replaced.
        new_pdf = pdf_bytes("S00478-reposted-with-new-date")
        m = move(500, "INV/2026/00478", write_date=NEW, sent_at=OLD, checksum=hashlib.sha256(PDF).hexdigest())
        odoo = FakeInvoiceOdoo(self.one_order_tables(m), {500: new_pdf})
        sf = FakeOrgSF([{"Id": "00Pa", "ParentId": OPP_A, "Name": NAME, "BodyLength": len(PDF)}])
        failed = write_invoice_pdfs(sf, odoo, dry_run=False)
        self.assertEqual(failed, 0)
        self.assertEqual(sf.sent("POST"), [])  # no second attachment
        patches = sf.sent("PATCH")
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0][1], "/services/data/v68.0/sobjects/Attachment/00Pa")
        self.assertEqual(base64.b64decode(patches[0][2]["Body"]), new_pdf)
        self.assertEqual(len([a for a in sf.attachments if a["Name"] == NAME]), 1)
        self.assertEqual(odoo.tables["account.move"][0]["sf_pdf_checksum"], hashlib.sha256(new_pdf).hexdigest())

    def test_invoice_spanning_two_orders_is_attached_to_each_opportunity_once(self):
        m = move(500, "INV/2026/00478")
        odoo = FakeInvoiceOdoo(tables([m], [order(10, OPP_A), order(11, OPP_B)],
                                      [line(100, 10), line(101, 11)],
                                      [inv_line(1000, 500, [100]), inv_line(1001, 500, [101])]), {500: PDF})
        sf = FakeOrgSF()
        write_invoice_pdfs(sf, odoo, dry_run=False)
        self.assertEqual(odoo.renders, [500])  # rendered once
        posts = sf.sent("POST")
        self.assertEqual(sorted(p[2]["ParentId"] for p in posts), sorted([OPP_A, OPP_B]))
        self.assertEqual({p[2]["Name"] for p in posts}, {NAME})

    def test_credit_note_is_attached_when_refunds_are_on(self):
        m = move(600, "RINV/2026/00007", move_type="out_refund")
        odoo = FakeInvoiceOdoo(tables([m], [order(10, OPP_A)], [line(100, 10)], [inv_line(1000, 600, [100])]),
                               {600: pdf_bytes("credit")})
        sf = FakeOrgSF()
        write_invoice_pdfs(sf, odoo, dry_run=False)
        self.assertEqual([p[2]["Name"] for p in sf.sent("POST")], ["RINV-2026-00007.pdf"])

    def test_credit_note_is_left_out_when_refunds_are_off(self):
        m = move(600, "RINV/2026/00007", move_type="out_refund")
        odoo = FakeInvoiceOdoo(tables([m], [order(10, OPP_A)], [line(100, 10)], [inv_line(1000, 600, [100])]),
                               {600: pdf_bytes("credit")})
        sf = FakeOrgSF()
        with mock.patch.object(config, "SF_INVOICE_PDF_REFUNDS", False):
            write_invoice_pdfs(sf, odoo, dry_run=False)
        self.assertEqual(odoo.renders, [])
        self.assertEqual(sf.writes, [])

    def test_dry_run_writes_nothing(self):
        odoo = FakeInvoiceOdoo(self.one_order_tables(move(500, "INV/2026/00478")), {500: PDF})
        sf = FakeOrgSF()
        odoo_writes_before = len(odoo.log)
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            write_invoice_pdfs(sf, odoo, dry_run=True)
        self.assertEqual(sf.writes, [])  # nothing on Salesforce
        self.assertEqual([e for e in odoo.log[odoo_writes_before:] if e[0] in ("write", "create")], [])
        self.assertIn(f"[dry-run] would attach {NAME} on {OPP_A}", out.getvalue())
        self.assertIn("attached 1", out.getvalue())

    def test_opportunity_filter_limits_the_step(self):
        m1, m2 = move(500, "INV/2026/00478"), move(501, "INV/2026/00479")
        odoo = FakeInvoiceOdoo(tables([m1, m2], [order(10, OPP_A), order(11, OPP_B)],
                                      [line(100, 10), line(101, 11)],
                                      [inv_line(1000, 500, [100]), inv_line(1001, 501, [101])]),
                               {500: PDF, 501: pdf_bytes("other")})
        sf = FakeOrgSF()
        write_invoice_pdfs(sf, odoo, dry_run=False, opportunity=OPP_B)
        self.assertEqual([p[2]["ParentId"] for p in sf.sent("POST")], [OPP_B])

    def test_draft_and_unlinked_invoices_are_not_targets(self):
        draft = move(700, "INV/2026/00999")
        draft["state"] = "draft"
        odoo = FakeInvoiceOdoo(tables([draft], [order(10, OPP_A)], [line(100, 10)], [inv_line(1000, 700, [100])]),
                               {700: PDF})
        self.assertEqual(invoice_targets(odoo, None, True), {})


if __name__ == "__main__":
    unittest.main()
