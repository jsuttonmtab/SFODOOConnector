"""Line description (name) changes: reported in the line change note, or held with SF_HOLD_NAME_UPDATES.
No Odoo or Salesforce calls.

Run from the repo root:  python -m unittest connector.test_name_updates
"""
import unittest
from collections import Counter, defaultdict
from types import SimpleNamespace
from unittest import mock

from connector import config
from connector.sync_lines import LineSync, hold_name_changes


class NoRowsOdoo:
    """Every search returns nothing (no currency rows: the default currency format is used)."""

    def search_read(self, model, domain, fields, **kwargs):
        return []


def make_line_sync():
    parent = SimpleNamespace(odoo=NoRowsOdoo(), dry_run=False, counts=Counter(), issues=defaultdict(list),
                             warn=lambda *args: None)
    return LineSync(parent)


SF_LINE = {"Id": "00k1", "Product2": {"ProductCode": "W4", "Name": "Data Conversion - Syndicated Database"}}
NAME_CHANGE = ("name", "2025 W4 USA NVES", "2025 W4 USA")
QTY_CHANGE = ("product_uom_qty", 2.0, 3.0)


class HoldNameChangesTests(unittest.TestCase):
    def test_default_writes_the_name(self):
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", False):
            write, held = hold_name_changes([NAME_CHANGE, QTY_CHANGE])
        self.assertEqual(write, [NAME_CHANGE, QTY_CHANGE])
        self.assertEqual(held, [])

    def test_hold_keeps_the_name_out_of_the_write_and_reports_it(self):
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", True):
            write, held = hold_name_changes([NAME_CHANGE, QTY_CHANGE])
        self.assertEqual(write, [QTY_CHANGE])  # quantity still written
        self.assertEqual(held, [NAME_CHANGE])

    def test_hold_with_only_a_name_change_writes_nothing(self):
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", True):
            write, held = hold_name_changes([NAME_CHANGE])
        self.assertEqual(write, [])
        self.assertEqual(held, [NAME_CHANGE])


class DescriptionNoteTests(unittest.TestCase):
    def test_name_change_is_listed_as_description_old_to_new(self):
        text = make_line_sync().changed_text({}, [NAME_CHANGE], SF_LINE, "GBP")
        self.assertIn("description: 2025 W4 USA NVES → 2025 W4 USA", text)

    def test_name_only_update_still_gets_a_note_line(self):
        text = make_line_sync().changed_text({}, [NAME_CHANGE], SF_LINE, "GBP")
        self.assertIsNotNone(text)
        self.assertTrue(text.startswith("Data Conversion - Syndicated Database — "))

    def test_name_and_quantity_are_both_listed(self):
        text = make_line_sync().changed_text({}, [NAME_CHANGE, QTY_CHANGE], SF_LINE, "GBP")
        self.assertIn("description: 2025 W4 USA NVES → 2025 W4 USA", text)
        self.assertIn("quantity 2 → 3", text)


if __name__ == "__main__":
    unittest.main()
