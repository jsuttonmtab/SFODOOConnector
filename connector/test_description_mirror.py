"""Item Description mirrors the Salesforce description. sf_combined_description and item_description are written in
the same call, so the hold and the lock act on both. sf_study_notes gets the study notes (line only). The backfill
migration copies sf_combined_description to item_description on synced lines. No Odoo or Salesforce calls.

Run from the repo root:  python -m unittest connector.test_description_mirror
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from connector import config
from connector.sync_lines import LineSync, description_vals, hold_name_changes

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "odoo_addons" / "sf_connector" / "migrations" / "19.0.1.8.11" / "post-migrate.py"


class DescriptionValsTests(unittest.TestCase):
    def test_combined_goes_to_both_fields_trimmed(self):
        vals = description_vals({"Combined_Product_Description__c": "  2026 4 ZZ Test  "})
        self.assertEqual(vals["sf_combined_description"], "2026 4 ZZ Test")
        self.assertEqual(vals["item_description"], "2026 4 ZZ Test")

    def test_study_notes_go_only_to_sf_study_notes(self):
        vals = description_vals({"Combined_Product_Description__c": "2026 Q1 USA",
                                 "Study_Notes_Additional_Description__c": "(inc Segmentation)"})
        self.assertEqual(vals["sf_study_notes"], "(inc Segmentation)")
        self.assertEqual(vals["item_description"], "2026 Q1 USA")  # not the study notes

    def test_blank_values_are_false(self):
        vals = description_vals({"Combined_Product_Description__c": "   ", "Study_Notes_Additional_Description__c": None})
        self.assertEqual(vals, {"sf_combined_description": False, "item_description": False, "sf_study_notes": False})


class HoldTests(unittest.TestCase):
    CHANGES = [("product_uom_qty", 1.0, 2.0), ("sf_combined_description", "old", "new"), ("item_description", "old", "new")]

    def test_hold_on_holds_both_description_fields_and_nothing_else(self):
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", True):
            write, held = hold_name_changes(list(self.CHANGES))
        self.assertEqual(write, [("product_uom_qty", 1.0, 2.0)])
        self.assertEqual({field for field, _, _ in held}, {"sf_combined_description", "item_description"})

    def test_hold_off_writes_both(self):
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", False):
            write, held = hold_name_changes(list(self.CHANGES))
        self.assertEqual(held, [])
        self.assertEqual(len(write), 3)

    def test_item_description_alone_is_not_held(self):
        # The two already differ in Odoo, so the item change brings item_description into line. Not a description change.
        changes = [("item_description", "study notes", "2026 Q1 USA")]
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", True):
            write, held = hold_name_changes(changes)
        self.assertEqual(write, changes)
        self.assertEqual(held, [])


class LockTests(unittest.TestCase):
    """LineSync.apply_locks, run on a stand-in for the sync object (only line_error and parent.warn are used)."""

    def run_locks(self, rec, vals):
        class Parent:
            def __init__(self):
                self.warnings = []

            def warn(self, kind, detail):
                self.warnings.append(kind)

        stub = types.SimpleNamespace(parent=Parent(), line_error=lambda *a: None)
        LineSync.apply_locks(stub, rec, vals, {"Id": "006", "Name": "Deal"}, {"Id": "00k"})
        return vals, stub.parent.warnings

    def test_delivered_line_drops_the_description_and_its_mirror(self):
        rec = {"qty_delivered": 1, "deliverable_delivered_count": 0, "qty_invoiced": 0, "sf_combined_description": "old",
               "item_description": "old", "product_uom_qty": 1, "price_unit": 0, "sf_billing_schedule": False, "name": "P"}
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", False):
            vals, warnings = self.run_locks(rec, {"sf_combined_description": "new", "item_description": "new"})
        self.assertEqual(vals, {})
        self.assertEqual(warnings, ["description not updated (line is delivered or invoiced)"])

    def test_item_only_mismatch_on_an_invoiced_line_is_brought_into_line(self):
        # Display-only and copies Odoo's own combined value: not locked.
        rec = {"qty_delivered": 0, "deliverable_delivered_count": 0, "qty_invoiced": 2, "sf_combined_description": "same",
               "item_description": "stale", "product_uom_qty": 2, "price_unit": 5, "sf_billing_schedule": False, "name": "P"}
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", False):
            vals, _ = self.run_locks(rec, {"sf_combined_description": "same", "item_description": "same"})
        self.assertEqual(vals.get("item_description"), "same")


class MigrationTests(unittest.TestCase):
    """The backfill, run against a stand-in for the odoo module and for the order lines."""

    class Line:
        def __init__(self, item, combined):
            self.item_description = item
            self.sf_combined_description = combined
            self.writes = []

        def write(self, vals):
            self.writes.append(vals)
            for k, v in vals.items():
                setattr(self, k, v)

    def load(self, lines):
        class Model:
            def search(self, domain):
                assert ("sf_line_id", "!=", False) in domain
                return lines

        class Env(dict):
            pass

        env = Env({"sale.order.line": Model()})
        fake_odoo = types.ModuleType("odoo")
        fake_odoo.SUPERUSER_ID = 1
        fake_odoo.api = types.SimpleNamespace(Environment=lambda cr, uid, ctx: env)
        spec = importlib.util.spec_from_file_location("post_migrate_19_0_1_8_11", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"odoo": fake_odoo}):
            spec.loader.exec_module(module)
            return module.migrate(None, "19.0.1.8.11")

    def test_backfill_copies_the_combined_description_and_counts_changes(self):
        lines = [self.Line("study notes", "2026 Q1 USA"), self.Line("2026 Q1 USA", "2026 Q1 USA"),
                 self.Line("study notes", None), self.Line(False, False)]
        changed = self.load(lines)
        self.assertEqual(changed, 2)
        self.assertEqual(lines[0].item_description, "2026 Q1 USA")
        self.assertEqual(lines[0].writes, [{"item_description": "2026 Q1 USA"}])
        self.assertEqual(lines[1].writes, [])  # already equal: not touched
        self.assertEqual(lines[2].item_description, False)  # no Salesforce description: empty
        self.assertEqual(lines[3].writes, [])


if __name__ == "__main__":
    unittest.main()
