"""Delivered counter (SF_DELIVERED_FIELD), the fulfilment rules, and the description lock (warning, held).
Fakes only: no Odoo or Salesforce calls.

Run from the repo root:  python -m unittest connector.test_fulfilment_locks
"""
import base64
import io
import unittest
from collections import Counter, defaultdict
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from connector import config
from connector.sf_writeback import WriteBack, _line_values
from connector.sync_lines import LineSync
from connector.test_finance_review import FakeOdoo
from connector.test_sf_writeback import FakeOrg, ORG_FIELDS, _org_field

COUNTER = "Fulfillment_Quantity__c"
LINE_ID = "00kA"


def org_with_counter(counter_type="double", updateable=True):
    """An org whose OpportunityLineItem has the delivered counter, with the given type and updateability."""
    fields = dict(ORG_FIELDS)
    fields["OpportunityLineItem"] = list(ORG_FIELDS["OpportunityLineItem"]) + [
        {"name": COUNTER, "type": counter_type, "updateable": updateable}]
    sf = FakeOrg(rows={LINE_ID: {"Id": LINE_ID, "Odoo_Line_Id__c": 938.0, "Odoo_Qty_Delivered__c": 0.0,
                                 "Odoo_Qty_Invoiced__c": 1.0, "Odoo_Fulfilled__c": False, COUNTER: None}})
    sf.describe = lambda sobject: {"fields": fields[sobject]}
    return sf


def deliverable_rec(**over):
    rec = {"id": 938, "qty_invoiced": 4.0, "product_uom_qty": 4.0, "qty_delivered": 1.0,
           "deliverable_count": 4, "deliverable_delivered_count": 1}
    rec.update(over)
    return rec


class CounterWriteTests(unittest.TestCase):
    def test_default_setting_writes_no_counter(self):
        with mock.patch.object(config, "SF_DELIVERED_FIELD", ""):
            self.assertNotIn(COUNTER, _line_values(deliverable_rec()))

    def test_counter_gets_the_delivered_value_and_the_org_name(self):
        sf = org_with_counter("double")
        with mock.patch.object(config, "SF_DELIVERED_FIELD", COUNTER):
            wb = WriteBack(sf, None, dry_run=False)
            self.assertTrue(wb._fields_ok("OpportunityLineItem"))
            current = wb._current("OpportunityLineItem", [LINE_ID])
            wb._plan("OpportunityLineItem", LINE_ID, "", current[LINE_ID], _line_values(deliverable_rec()), group="006A")
        payload = wb.pending["OpportunityLineItem"][0][1]
        self.assertEqual(payload[COUNTER], 1)
        self.assertEqual(payload["Odoo_Qty_Delivered__c"], 1)  # the Odoo field keeps its value
        self.assertIn(COUNTER, sf.soql[0])

    def test_text_counter_is_written_as_text(self):
        sf = org_with_counter("string")
        with mock.patch.object(config, "SF_DELIVERED_FIELD", COUNTER):
            wb = WriteBack(sf, None, dry_run=False)
            wb._fields_ok("OpportunityLineItem")
            current = wb._current("OpportunityLineItem", [LINE_ID])
            rec = deliverable_rec(deliverable_delivered_count=0.5, qty_delivered=0.5)
            wb._plan("OpportunityLineItem", LINE_ID, "", current[LINE_ID], _line_values(rec), group="006A")
        self.assertEqual(wb.pending["OpportunityLineItem"][0][1][COUNTER], "0.5")

    def test_counter_already_equal_is_not_planned_whether_text_or_number(self):
        for counter_type, stored, expected in (("double", 1.0, 0), ("string", "1", 0), ("string", "2", 1)):
            sf = org_with_counter(counter_type)
            # the row already holds every value this line would be sent, so only the counter can differ
            sf.rows[LINE_ID].update({"Odoo_Qty_Delivered__c": 1.0, "Odoo_Qty_Invoiced__c": 4.0, COUNTER: stored})
            with mock.patch.object(config, "SF_DELIVERED_FIELD", COUNTER):
                wb = WriteBack(sf, None, dry_run=False)
                wb._fields_ok("OpportunityLineItem")
                current = wb._current("OpportunityLineItem", [LINE_ID])
                wb._plan("OpportunityLineItem", LINE_ID, "", current[LINE_ID], _line_values(deliverable_rec()),
                         group="006A")
            self.assertEqual(wb.counts[("OpportunityLineItem", "planned")], expected, (counter_type, stored))

    def test_counter_that_is_not_updateable_stops_the_object(self):
        sf = org_with_counter("double", updateable=False)
        with mock.patch.object(config, "SF_DELIVERED_FIELD", COUNTER):
            out = io.StringIO()
            with redirect_stdout(out):
                ok = WriteBack(sf, None, dry_run=False)._fields_ok("OpportunityLineItem")
        self.assertFalse(ok)
        self.assertIn(f"{COUNTER} is not updateable", out.getvalue())

    def test_counter_that_does_not_exist_stops_the_object(self):
        sf = org_with_counter()
        sf.describe = lambda sobject: {"fields": ORG_FIELDS[sobject]}  # the counter is not in the org
        with mock.patch.object(config, "SF_DELIVERED_FIELD", COUNTER):
            out = io.StringIO()
            with redirect_stdout(out):
                ok = WriteBack(sf, None, dry_run=True)._fields_ok("OpportunityLineItem")
        self.assertFalse(ok)
        self.assertIn(f"{COUNTER} does not exist", out.getvalue())


class FulfilmentRuleTests(unittest.TestCase):
    def test_deliverable_line_is_delivered_by_its_delivered_count(self):
        v = _line_values(deliverable_rec(deliverable_delivered_count=1, deliverable_count=4))
        self.assertEqual(v["Odoo_Qty_Delivered__c"], 1)
        self.assertFalse(v["Odoo_Fulfilled__c"])

    def test_deliverable_line_is_fulfilled_when_all_deliverables_are_delivered(self):
        v = _line_values(deliverable_rec(deliverable_delivered_count=4, deliverable_count=4, qty_delivered=0))
        self.assertEqual(v["Odoo_Qty_Delivered__c"], 4)  # the deliverable count, not qty_delivered
        self.assertTrue(v["Odoo_Fulfilled__c"])

    def test_subscription_delivered_is_qty_delivered_and_fulfilled_when_invoiced(self):
        sub = {"id": 1, "qty_invoiced": 1.0, "product_uom_qty": 1.0, "qty_delivered": 0.0,
               "deliverable_count": 0, "deliverable_delivered_count": 0}
        v = _line_values(sub)
        self.assertEqual(v["Odoo_Qty_Delivered__c"], 0)
        self.assertTrue(v["Odoo_Fulfilled__c"])
        self.assertFalse(_line_values(dict(sub, qty_invoiced=0.0))["Odoo_Fulfilled__c"])


class DescriptionLockTests(unittest.TestCase):
    """An invoiced line whose Salesforce description changed: with the hold on, nothing is raised. With the hold off,
    one warning per run (not an error), the description is not written, and the rest of the line still syncs."""

    def sync_with(self):
        odoo = FakeOdoo({})
        parent = SimpleNamespace(odoo=odoo, dry_run=False, counts=Counter(), issues=defaultdict(list),
                                 warn=lambda *args: self.warnings.append(args))
        self.warnings = []
        return LineSync(parent)

    def rec(self):
        return {"id": 938, "sf_line_id": LINE_ID, "product_id": [1, "P"], "name": "P",
                "product_uom_qty": 2.0, "qty_delivered": 0.0, "deliverable_delivered_count": 0,
                "qty_invoiced": 2.0, "sf_combined_description": "old text", "price_unit": 10.0,
                "sf_billing_schedule": False}

    def vals(self):
        return {"product_uom_qty": 3.0, "sf_combined_description": "new text", "name": "P", "price_unit": 10.0,
                "sf_billing_schedule": False}

    def opp(self):
        return {"Id": "006A", "Name": "Acme"}

    def line(self):
        return {"Id": LINE_ID}

    def test_hold_on_raises_no_lock_message_and_still_applies_the_quantity(self):
        sync = self.sync_with()
        vals = self.vals()
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", True):
            sync.apply_locks(self.rec(), vals, self.opp(), self.line())
        self.assertEqual(sync.counts["errors"], 0)
        self.assertEqual(self.warnings, [])
        self.assertEqual(vals["product_uom_qty"], 3.0)  # the quantity is still applied

    def test_hold_off_warns_once_not_error_and_keeps_the_rest_of_the_line(self):
        sync = self.sync_with()
        vals = self.vals()
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", False):
            sync.apply_locks(self.rec(), vals, self.opp(), self.line())
        self.assertEqual(sync.counts["errors"], 0)  # no error on the opportunity
        self.assertNotIn("sf_combined_description", vals)  # the description is not written
        self.assertEqual(vals["product_uom_qty"], 3.0)  # the quantity is applied
        self.assertEqual(len(self.warnings), 1)
        self.assertEqual(self.warnings[0][0], "description not updated (line is delivered or invoiced)")

    def test_hold_off_and_description_unchanged_raises_nothing(self):
        sync = self.sync_with()
        vals = self.vals()
        vals["sf_combined_description"] = "old text"
        with mock.patch.object(config, "SF_HOLD_NAME_UPDATES", False):
            sync.apply_locks(self.rec(), vals, self.opp(), self.line())
        self.assertEqual(self.warnings, [])


if __name__ == "__main__":
    unittest.main()
