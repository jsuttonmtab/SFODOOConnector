"""The invoice line text: an old-format line (name = product + description, before 19.0.1.8.0) does not print the
description twice; a new-format line prints as before; a hand-edited line prints as Finance wrote it. The model is loaded
against a stand-in for odoo; no Odoo needed.

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


class Line:
    def __init__(self, name, description):
        self.name = name
        self.sf_combined_description = description


class LineTextTests(unittest.TestCase):
    def setUp(self):
        self.AccountMoveLine = load_line_model()

    def text(self, name, description):
        return self.AccountMoveLine.sf_item_text(Line(name, description))

    def test_old_format_name_that_is_the_description_prints_it_once(self):
        name = "Halo User Seat - Creator (3 seats)"
        self.assertEqual(self.text(name, name), ("", name))

    def test_old_format_product_plus_description_keeps_only_the_product(self):
        name = "Data Conversion - Proprietary Database\n" + DESC
        self.assertEqual(self.text(name, DESC), ("Data Conversion - Proprietary Database", DESC))

    def test_new_format_line_prints_as_before(self):
        self.assertEqual(self.text("Data Access", DESC), ("Data Access", DESC))

    def test_hand_edited_line_prints_as_written(self):
        self.assertEqual(self.text("Custom wording for Finance", DESC), ("Custom wording for Finance", DESC))

    def test_description_inside_the_name_is_removed_and_trimmed(self):
        self.assertEqual(self.text("Product 2026 USA Market Potential Study edited", DESC),
                         ("Product  edited", DESC))

    def test_no_description_prints_the_name_only(self):
        self.assertEqual(self.text("Data Access", False), ("Data Access", ""))
        self.assertEqual(self.text("Data Access", "   "), ("Data Access", ""))


if __name__ == "__main__":
    unittest.main()
