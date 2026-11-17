"""Salesforce description on its own field (sf_combined_description), the line name is the product name only,
SF_HOLD_NAME_UPDATES, and the line change note. No Odoo or Salesforce calls.

Run from the repo root:  python -m unittest connector.test_name_updates
"""
import unittest
from collections import Counter, defaultdict
from types import SimpleNamespace
from unittest import mock

from connector import config
from connector.sync_lines import LineSync, hold_name_changes
from connector.test_finance_review import FakeOdoo


def make_line_sync(tables=None):
    odoo = FakeOdoo(tables or {})
    parent = SimpleNamespace(odoo=odoo, dry_run=False, counts=Counter(), issues=defaultdict(list),
                             warn=lambda *args: None)
    return LineSync(parent)


SF_LINE = {"Id": "00k1", "Product2Id": "01tA",
           "Product2": {"ProductCode": "W4", "Name": "Data Conversion - Syndicated Database"},
           "Combined_Product_Description__c": "2025 W4 USA NVES", "Quantity": 2, "Total_Billable_Amount__c": 100}
DESCRIPTION_CHANGE = ("sf_combined_description", "2025 W4 USA NVES", "2025 W4 USA")
QTY_CHANGE = ("product_uom_qty", 2.0, 3.0)
PRODUCT_TABLES = {"product.template": [{"id": 10, "sf_product_id": "01tA"}],
                  "product.product": [{"id": 100, "default_code": "W4", "name": "Data Conversion - Syndicated Database",
                                       "product_tmpl_id": [10, "Data Conversion"]}]}


class LineValuesTests(unittest.TestCase):
    def test_name_is_the_product_name_and_description_has_its_own_field(self):
        sync = make_line_sync(PRODUCT_TABLES)
        vals = sync.line_vals(SF_LINE, {})
        self.assertEqual(vals["name"], "Data Conversion - Syndicated Database")
        self.assertEqual(vals["sf_combined_description"], "2025 W4 USA NVES")

    def test_without_a_product_name_the_description_is_used_as_the_name(self):
        sync = make_line_sync(PRODUCT_TABLES)
        line = dict(SF_LINE, Product2={"ProductCode": None, "Name": None})
        vals = sync.line_vals(line, {})
        self.assertEqual(vals["name"], "2025 W4 USA NVES")

    def test_a_line_with_neither_name_nor_description_is_a_problem(self):
        sync = make_line_sync(PRODUCT_TABLES)
        problems = {}
        line = dict(SF_LINE, Product2={"ProductCode": None, "Name": None}, Combined_Product_Description__c=None)
        sync.line_vals(line, problems)
        self.assertIn("name", problems)


class HoldTests(unittest.TestCase):
    def test_default_writes_the_description(self):
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", False):
            write, held = hold_name_changes([DESCRIPTION_CHANGE, QTY_CHANGE])
        self.assertEqual(write, [DESCRIPTION_CHANGE, QTY_CHANGE])
        self.assertEqual(held, [])

    def test_hold_keeps_the_description_out_of_the_write_and_reports_it(self):
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", True):
            write, held = hold_name_changes([DESCRIPTION_CHANGE, QTY_CHANGE])
        self.assertEqual(write, [QTY_CHANGE])  # quantity still written
        self.assertEqual(held, [DESCRIPTION_CHANGE])

    def test_hold_does_not_hold_the_product_name(self):
        # The product name is not the Salesforce description: a change to it is written.
        product_name = ("name", "Old product name", "Data Conversion - Syndicated Database")
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", True):
            write, held = hold_name_changes([product_name])
        self.assertEqual(write, [product_name])
        self.assertEqual(held, [])


class DescriptionNoteTests(unittest.TestCase):
    def test_description_change_is_listed_as_description_old_to_new(self):
        text = make_line_sync().changed_text({}, [DESCRIPTION_CHANGE], SF_LINE, "GBP")
        self.assertIn("description: 2025 W4 USA NVES → 2025 W4 USA", text)

    def test_description_only_update_still_gets_a_note_line(self):
        text = make_line_sync().changed_text({}, [DESCRIPTION_CHANGE], SF_LINE, "GBP")
        self.assertIsNotNone(text)
        self.assertTrue(text.startswith("Data Conversion - Syndicated Database — "))

    def test_description_and_quantity_are_both_listed(self):
        text = make_line_sync().changed_text({}, [DESCRIPTION_CHANGE, QTY_CHANGE], SF_LINE, "GBP")
        self.assertIn("description: 2025 W4 USA NVES → 2025 W4 USA", text)
        self.assertIn("quantity 2 → 3", text)


if __name__ == "__main__":
    unittest.main()
