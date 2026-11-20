"""The invoice line text: the line text and the Salesforce description are printed once when they are the same (ignoring
case, extra spaces and surrounding brackets); an old-format line (name = product + description, before 19.0.1.8.0) does not
print the description twice; a different description prints after the product name in brackets; a hand-edited line prints as
written. The model is loaded against a stand-in for odoo; no Odoo needed.

Run from the repo root:  python -m unittest connector.test_invoice_line_text
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

MODEL = Path(__file__).resolve().parent.parent / "odoo_addons" / "sf_connector" / "models" / "account_move_line.py"
DESC = "2026 USA Market Potential Study"
PRODUCT = "Halo User Seat - Creator"
HALO = "Halo User Seat - Creator (3 seats)"
DATA_CONVERSION = "Data Conversion - Proprietary Database"


class _Anything:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def load_line_model():
    odoo = types.ModuleType("odoo")
    odoo.fields = _Anything()
    odoo.models = types.SimpleNamespace(Model=object)
    with mock.patch.dict(sys.modules, {"odoo": odoo}):
        spec = importlib.util.spec_from_file_location("account_move_line_under_test", MODEL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module.AccountMoveLine


class Product:
    def __init__(self, name):
        self.name = name


class Line:
    def __init__(self, name, description, product=""):
        self.name = name
        self.sf_combined_description = description
        self.product_id = Product(product) if product else False


class LineTextTests(unittest.TestCase):
    def setUp(self):
        self.AccountMoveLine = load_line_model()

    def text(self, name, description, product=""):
        return self.AccountMoveLine.sf_item_text(Line(name, description, product))

    def test_identical_text_prints_once_as_plain_text(self):
        self.assertEqual(self.text(HALO, HALO, PRODUCT), (HALO, ""))

    def test_identical_ignoring_case_spaces_and_brackets_prints_once(self):
        self.assertEqual(self.text("halo  user seat - creator (3 seats)", "(Halo User Seat - Creator (3 seats))", PRODUCT),
                         ("halo  user seat - creator (3 seats)", ""))

    def test_old_format_name_that_is_the_whole_description_prints_it_once(self):
        # The description was the whole old-format name: stripping it leaves nothing, so the description prints plain.
        self.assertEqual(self.text(HALO, HALO), (HALO, ""))
        self.assertEqual(self.text("\n" + HALO, HALO, PRODUCT), (HALO, ""))

    def test_description_equal_to_the_product_name_prints_the_name_once(self):
        name = DATA_CONVERSION + "\n" + DATA_CONVERSION
        self.assertEqual(self.text(name, DATA_CONVERSION, DATA_CONVERSION), (DATA_CONVERSION, ""))

    def test_different_description_prints_product_then_bracketed_description(self):
        name = DATA_CONVERSION + "\n" + DESC
        self.assertEqual(self.text(name, DESC, DATA_CONVERSION), (DATA_CONVERSION, DESC))

    def test_new_format_line_with_a_different_description_prints_as_before(self):
        self.assertEqual(self.text("Data Access", DESC, "Data Access"), ("Data Access", DESC))

    def test_hand_edited_line_prints_as_written_with_the_description(self):
        self.assertEqual(self.text("Custom wording for Finance", DESC, PRODUCT),
                         ("Custom wording for Finance", DESC))

    def test_description_inside_the_name_is_removed_and_trimmed(self):
        self.assertEqual(self.text("Product " + DESC + " edited", DESC), ("Product  edited", DESC))

    def test_no_description_prints_the_name_only(self):
        self.assertEqual(self.text("Data Access", False), ("Data Access", ""))
        self.assertEqual(self.text("Data Access", "   "), ("Data Access", ""))


if __name__ == "__main__":
    unittest.main()
