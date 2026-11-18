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
                if var == "o" and f"o.{field}(" in expr:
                    continue  # a method call (sf_...()), checked by test_helper_methods...
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


if __name__ == "__main__":
    unittest.main()
