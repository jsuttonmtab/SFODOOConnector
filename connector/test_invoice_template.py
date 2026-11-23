"""Static checks on the invoice QWeb template (odoo_addons/sf_connector/report/report_invoice.xml).

Every field the template reads on the invoice (o.), the invoice line (line.) and the company (c.) must exist on that
model. The field lists come from the Odoo sandbox (connector/test_fixtures_odoo_fields.json). Every helper method the
template calls (o.sf_...()) must be defined in account_move.py. The logo is printed as it is, with no resizing (the
server's image library crashes on resize). No Odoo needed to run this.

Run from the repo root:  python -m unittest connector.test_invoice_template
"""
import json
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "odoo_addons" / "sf_connector" / "report" / "report_invoice.xml"
MODEL = ROOT / "odoo_addons" / "sf_connector" / "models" / "account_move.py"
FIELDS = json.loads((Path(__file__).resolve().parent / "test_fixtures_odoo_fields.json").read_text(encoding="utf-8"))

# Variable in the template -> the Odoo model it stands for.
VARIABLES = {"o": "account.move", "line": "account.move.line", "c": "res.company"}
PATH = re.compile(r"\b(o|line|c)\.([a-z_][a-z0-9_]*)")
CALL = re.compile(r"\bo\.(sf_[a-z_]+)\(")
QWEB_ATTRIBUTES = ("t-esc", "t-field", "t-value", "t-if", "t-foreach", "t-set", "t-att-src")


def template_expressions():
    """Every attribute value in the template that is QWeb code."""
    root = ET.parse(TEMPLATE).getroot()
    for element in root.iter():
        for name, value in element.attrib.items():
            if name in QWEB_ATTRIBUTES:
                yield value


class InvoiceTemplateTests(unittest.TestCase):
    def setUp(self):
        self.expressions = list(template_expressions())
        self.text = TEMPLATE.read_text(encoding="utf-8")

    def test_every_invoice_field_read_by_the_template_exists_on_its_model(self):
        missing = []
        for expr in self.expressions:
            for var, field in PATH.findall(expr):
                if f"{var}.{field}(" in expr:
                    continue  # a method call (sf_...()), checked by the helper tests
                if field not in FIELDS[VARIABLES[var]]:
                    missing.append((VARIABLES[var], field, expr[:70]))
        self.assertEqual(missing, [], f"the template reads fields Odoo does not have: {missing}")

    def test_partner_and_line_fields_exist(self):
        partner = set(re.findall(r"o\.partner_id\.([a-z_][a-z0-9_]*)", self.text))
        self.assertTrue(partner <= set(FIELDS["res.partner"]), partner - set(FIELDS["res.partner"]))
        self.assertIn("price_subtotal", FIELDS["sale.order.line"])  # "Total cost" (LLC), the sale line subtotal
        self.assertIn("price_subtotal", FIELDS["account.move.line"])
        self.assertIn("invoice_payment_term_id", FIELDS["account.move"])
        self.assertIn("name", FIELDS["account.payment.term"])

    def test_helper_methods_the_template_calls_are_defined_on_the_model(self):
        defined = set(re.findall(r"def (sf_[a-z_]+)\(", MODEL.read_text(encoding="utf-8")))
        called = set(CALL.findall(self.text))
        self.assertTrue(called, "the template should call at least one sf_ helper")
        self.assertTrue(called <= defined, f"the template calls methods that do not exist: {called - defined}")

    def test_sale_order_only_fields_are_not_read_from_the_invoice(self):
        # client_order_ref is a sale.order field. The invoice reads it through sf_po_number(), never directly.
        self.assertNotIn("client_order_ref", self.text)
        self.assertNotIn("sale_order_ids", self.text)

    def test_logo_is_printed_as_is_without_resizing(self):
        self.assertIn("image_data_uri(c.logo)", self.text)
        self.assertNotRegex(self.text, r"(?i)resiz|max_width=|scale\(")


def paperformat(xmlid):
    """The fields of one report.paperformat record, by xmlid, as {field: value}."""
    root = ET.parse(TEMPLATE).getroot()
    for record in root.iter("record"):
        if record.get("id") == xmlid:
            return {f.get("name"): (f.get("eval") or f.text) for f in record.findall("field")}
    raise AssertionError(f"no paperformat {xmlid}")


class FooterAndLayoutTests(unittest.TestCase):
    """Page footer for both company layouts: the company footer text and "Page x of y", at the bottom of every page."""

    def setUp(self):
        self.text = TEMPLATE.read_text(encoding="utf-8")
        root = ET.parse(TEMPLATE).getroot()
        self.layout = next(t for t in root.iter("template") if t.get("id") == "sf_invoice_layout")
        self.document = next(t for t in root.iter("template") if t.get("id") == "report_sf_invoice_document")

    def test_layout_footer_prints_the_company_footer_and_page_x_of_y(self):
        footer = next(d for d in self.layout.iter("div") if "footer" in (d.get("class") or "").split())
        footer_xml = ET.tostring(footer, encoding="unicode")
        self.assertIn("o.company_id.sf_footer_html", footer_xml)
        self.assertIn('class="page"', footer_xml)
        self.assertIn('class="topage"', footer_xml)
        self.assertIn("Page", footer_xml)

    def test_layout_has_no_default_header_and_does_not_borrow_internal_layout(self):
        self.assertNotIn("web.internal_layout", self.text)
        header = next(d for d in self.layout.iter("div") if d.get("class") == "header")
        self.assertEqual(len(list(header)), 0)
        self.assertEqual((header.text or "").strip(), "")

    def test_footer_is_used_for_both_company_layouts(self):
        # The layout call is not inside a gamma/llc condition: one footer for both.
        calls = [el for el in self.document.iter() if el.get("t-call") == "sf_connector.sf_invoice_layout"]
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0].get("t-if"))

    def test_company_footer_is_not_printed_twice(self):
        body = [el for el in self.document.iter() if el.get("t-field") == "c.sf_footer_html"]
        self.assertEqual(body, [], "the company footer belongs in the page footer, not the body")

    def test_top_and_bottom_margins_fit_the_footer(self):
        letter, a4 = paperformat("paperformat_sf_letter"), paperformat("paperformat_sf_a4")
        for name, fmt in (("letter", letter), ("a4", a4)):
            self.assertEqual(float(fmt["margin_top"]), 12, name)
            self.assertGreaterEqual(float(fmt["margin_bottom"]), 36, name)  # room for the footer lines and page line
            self.assertNotIn("footer_spacing", fmt, name)  # not a field in Odoo 19: the record would not load

    def test_long_lines_table_header_repeats_on_each_page(self):
        self.assertIn("table-header-group", self.text)


class PaperFormatFieldTests(unittest.TestCase):
    """Every field the paper formats set must exist on report.paperformat (the list is saved from the sandbox)."""

    def test_paper_format_fields_exist_in_odoo(self):
        known = set(FIELDS["report.paperformat"])
        root = ET.parse(TEMPLATE).getroot()
        for record in root.iter("record"):
            if record.get("model") != "report.paperformat":
                continue
            used = {f.get("name") for f in record.findall("field")}
            self.assertEqual(used - known, set(), f"{record.get('id')} sets fields Odoo does not have")


class NoWrapAndStyleTests(unittest.TestCase):
    def setUp(self):
        self.text = TEMPLATE.read_text(encoding="utf-8")

    def test_due_date_and_early_payment_rows_do_not_wrap(self):
        self.assertIn(".sf-inv .sf-nowrap { white-space: nowrap; }", self.text)
        self.assertGreaterEqual(self.text.count('class="sf-nowrap"'), 3)  # due date cell and two offer rows (x2 cells)

    def test_bill_to_address_is_not_italic(self):
        self.assertIn(".sf-inv address { font-style: normal; margin: 0; }", self.text)
        self.assertIn('style="font-style: normal;"', self.text)


class FontSizeAndWebsiteTests(unittest.TestCase):
    """Sizes measured on the NetSuite reference: body, labels, table, totals and footer 8 pt rendered; title 12.8 pt;
    total 19.2 pt. The template sets 9.5 pt CSS body and footer (about 8 pt rendered), 15 pt title (12.8 pt) and 22.7 pt
    total (about 19.2 pt). wkhtmltopdf renders at about 0.84 to 0.85 of the CSS value."""

    def setUp(self):
        self.text = TEMPLATE.read_text(encoding="utf-8")

    def test_body_is_9_35_pt(self):
        self.assertIn(".sf-inv td, .sf-inv th, .sf-inv div, .sf-inv span, .sf-inv address { font-size: 9.35pt; }", self.text)
        self.assertIn("<div class=\"sf-inv\" style=\"font-family: 'Noto Sans', 'Nimbus Sans', sans-serif; font-size: 9.35pt;", self.text)

    def test_footer_is_9_35_pt_and_has_no_grid_lines(self):
        self.assertIn("<div class=\"footer sf-footer\" style=\"font-family: 'Noto Sans', 'Nimbus Sans', sans-serif; font-size: 9.35pt;", self.text)
        self.assertIn(".sf-footer table, .sf-footer td, .sf-footer th { border: 0 !important;", self.text)

    def test_title_and_total_sizes(self):
        self.assertIn(".sf-inv .sf-title { font-size: 15pt; }", self.text)
        self.assertIn(".sf-inv .sf-total { font-size: 22.7pt; }", self.text)
        self.assertIn(".sf-inv .sf-total span { font-size: 22.7pt; }", self.text)  # the amount span must carry the size
        self.assertIn('<div class="sf-total"><span', self.text)

    def test_website_is_printed_without_the_scheme(self):
        self.assertIn("o.sf_company_website()", self.text)
        self.assertNotIn('t-esc="c.website"', self.text)  # the raw field (with http://) is never printed


class CharsetFontAndLayoutRulesTests(unittest.TestCase):
    """Charset, font, Bill To, Tax Reg. label, row rules and keep-together rules (reviewed on the NetSuite references)."""

    def setUp(self):
        self.text = TEMPLATE.read_text(encoding="utf-8")

    def test_report_declares_utf8(self):
        self.assertIn('<meta charset="utf-8"/>', self.text)

    def test_font_is_noto_sans_first_and_lato_is_not_used(self):
        self.assertEqual(self.text.count("'Noto Sans', 'Nimbus Sans', sans-serif"), 4)  # body, body CSS, footer, footer CSS
        self.assertNotIn("Lato", self.text)

    def test_bill_to_prints_company_once_and_contact_only_when_it_differs(self):
        self.assertIn("o.commercial_partner_id.name", self.text)
        self.assertIn('o.partner_id.name != o.commercial_partner_id.name', self.text)
        self.assertIn('"fields": ["address"]', self.text)  # the address widget does not print the name again
        self.assertNotIn('"fields": ["name", "address"]', self.text)

    def test_tax_reg_label_only_when_there_is_a_value(self):
        self.assertRegex(self.text, r'<t t-if="o\.partner_id\.vat">\s*<div[^>]*>Tax Reg\. Number</div>')

    def test_row_rules_are_only_on_llc(self):
        self.assertIn("t-att-style=\"None if gamma else 'border-top: 1px solid #e3e8ee;'\"", self.text)

    def test_totals_early_payment_notes_and_payment_are_kept_together(self):
        self.assertIn('<table class="sf-keep" style="width: 46%', self.text)
        self.assertGreaterEqual(self.text.count('class="sf-keep"'), 4)
        self.assertIn(".sf-inv tr { page-break-inside: avoid; }", self.text)


class CombinedDescriptionTests(unittest.TestCase):
    """The Salesforce description on an invoice line: trimmed, and no brackets when it is empty or only spaces."""

    def setUp(self):
        self.text = TEMPLATE.read_text(encoding="utf-8")

    def test_description_comes_from_the_name_helper_and_is_trimmed_there(self):
        self.assertIn('<t t-set="sf_item" t-value="line.sf_item_text()"/>', self.text)
        self.assertIn('<t t-if="sf_item[1]"><br/><i><span t-esc="sf_item[1]"/></i></t>', self.text)

    def test_description_is_on_its_own_line_in_italics_without_brackets(self):
        cell = re.search(r'<t t-set="sf_item".*?</td>', self.text, re.S).group(0)
        self.assertIn('<span t-esc="sf_item[0]"/>', cell)  # line 1: the product or line text, as before
        self.assertIn('<br/><i><span t-esc="sf_item[1]"/></i>', cell)  # line 2: the description, italic
        self.assertNotIn("(<span", cell)  # no brackets around the description
        self.assertNotIn("font-size", cell)  # same size as the body: no smaller style on line 2

    def test_amount_cells_are_top_aligned_so_they_sit_on_line_one(self):
        self.assertIn(".sf-inv td, .sf-inv th { padding: 2px 4px; vertical-align: top; }", self.text)
        rows = re.findall(r"<tr t-foreach=\"lines\".*?</tr>", self.text, re.S)
        self.assertEqual(len(rows), 1)
        # Item cell, then the LLC amount cells (two) and the G.A.M.M.A amount cell (one): both branches share the row.
        self.assertEqual(rows[0].count("<td"), 4)
        self.assertNotIn("vertical-align: middle", rows[0])
        self.assertNotIn("vertical-align: bottom", rows[0])

    def test_line_rows_never_split_across_pages(self):
        # Taller rows (two-line text) must still move whole to the next page.
        self.assertIn(".sf-inv tr { page-break-inside: avoid; }", self.text)
        self.assertNotIn("page-break-inside: auto", self.text)
        self.assertIn('<thead style="display: table-header-group;">', self.text)  # header repeats on the next page

    def test_line_text_is_printed_once_for_both_layouts(self):
        # One line cell in the document, shared by mTab LLC and G.A.M.M.A (no duplicate in a layout branch).
        self.assertEqual(self.text.count("line.sf_item_text()"), 1)
        self.assertNotIn('t-field="line.name"', self.text)
        self.assertNotIn('t-if="line.sf_combined_description"', self.text)


class WebsiteHelperTests(unittest.TestCase):
    def test_helper_is_defined_on_the_invoice_model(self):
        defined = set(re.findall(r"def (sf_[a-z_]+)\(", MODEL.read_text(encoding="utf-8")))
        self.assertIn("sf_company_website", defined)


if __name__ == "__main__":
    unittest.main()
