"""Tests for the write-back splitting and failure report (sf_writeback.py). No Salesforce or Odoo calls:
a fake client answers the composite PATCH the way Salesforce does.

Run from the repo root:  python -m unittest connector.test_sf_writeback
"""
import io
import re
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from connector.sf_client import SalesforceError
from connector.sf_writeback import BATCH, INTEGRITY, WriteBack, _field_map, _opportunity_ids

BAD_OPP = "006AAAAAAAAAAAAAAA"
GOOD_OPP = "006BBBBBBBBBBBBBBB"


class FakeSF:
    """Salesforce composite PATCH. A call containing any bad record fails every record in it with an
    integrity error (as observed in the sandbox). per_record maps an Id to its own error. whole_fail:
    Ids whose presence makes the whole call raise (a request-level failure)."""

    api_version = "68.0"

    def __init__(self, bad=(), per_record=None, whole_fail=()):
        self.bad = set(bad)
        self.per_record = dict(per_record or {})
        self.whole_fail = set(whole_fail)
        self.calls = []  # list of Id lists, one per call

    def request(self, method, path, body):
        records = body["records"]
        assert body["allOrNone"] is False, "collections call must use allOrNone=false"
        ids = [r["Id"] for r in records]
        self.calls.append(ids)
        if any(i in self.whole_fail for i in ids):
            raise SalesforceError("HTTP 500 from Salesforce: boom")
        if any(i in self.bad for i in ids):
            message = f"field integrity exception: unknown ({BAD_OPP}|1|95190|566244|16|620,...)"
            # Salesforce returns no Id for a failed result, so results carry id None.
            return [{"id": None, "success": False,
                     "errors": [{"statusCode": "FIELD_INTEGRITY_EXCEPTION", "message": message}]} for _ in ids]
        results = []
        for i in ids:
            if i in self.per_record:
                results.append({"id": None, "success": False,
                                "errors": [{"statusCode": self.per_record[i], "message": "rejected"}]})
            else:
                results.append({"id": i, "success": True, "errors": []})
        return results


def account_ids(n):
    return [f"001{k:015d}" for k in range(n)]


def plan_accounts(wb, ids):
    """Queue Account records the way accounts() does, without the Odoo reads."""
    for sf_id in ids:
        wb.labels[("Account", sf_id)] = f"Name {sf_id}"
        wb.counts[("Account", "planned")] += 1
        wb.pending["Account"].append((sf_id, {"attributes": {"type": "Account"}, "Id": sf_id, "Odoo_ID__c": 1}))
    wb._flush("Account")


def written(wb, sobject):
    return wb.counts[(sobject, "planned")] - wb.counts[(sobject, "failed")]


def failed_ids(wb):
    return [row["id"] for row in wb.failed]


class SplitTests(unittest.TestCase):
    def run_accounts(self, sf, ids):
        wb = WriteBack(sf, None, dry_run=False)
        plan_accounts(wb, ids)
        return wb

    def test_batch_of_200_with_one_bad_record(self):
        ids = account_ids(BATCH)
        bad = ids[137]
        sf = FakeSF(bad={bad})
        wb = self.run_accounts(sf, ids)
        self.assertEqual(written(wb, "Account"), BATCH - 1)
        self.assertEqual(failed_ids(wb), [bad])
        self.assertEqual(wb.failed[0]["code"], INTEGRITY)
        self.assertLess(len(sf.calls), 2 * BATCH)  # isolated by halving, not one call per record

    def test_batch_of_200_with_three_bad_records(self):
        ids = account_ids(BATCH)
        bad = {ids[3], ids[90], ids[199]}
        sf = FakeSF(bad=bad)
        wb = self.run_accounts(sf, ids)
        self.assertEqual(written(wb, "Account"), BATCH - 3)
        self.assertEqual(set(failed_ids(wb)), bad)

    def test_batch_of_200_all_bad(self):
        ids = account_ids(BATCH)
        sf = FakeSF(bad=set(ids))
        wb = self.run_accounts(sf, ids)
        self.assertEqual(written(wb, "Account"), 0)
        self.assertEqual(failed_ids(wb), ids)  # every record reported, each with its own Id
        self.assertTrue(all(row["name"].startswith("Name ") for row in wb.failed))

    def test_failure_reported_with_id_not_none(self):
        ids = account_ids(5)
        sf = FakeSF(per_record={ids[1]: "INVALID_FIELD"})
        wb = self.run_accounts(sf, ids)
        self.assertEqual(len(sf.calls), 1)  # a per-record error needs no split
        self.assertEqual(failed_ids(wb), [ids[1]])
        self.assertEqual(wb.failed[0]["code"], "INVALID_FIELD")
        self.assertEqual(wb.failed[0]["name"], f"Name {ids[1]}")

    def test_whole_call_failure_isolates_the_record(self):
        ids = account_ids(10)
        sf = FakeSF(whole_fail={ids[5]})
        wb = self.run_accounts(sf, ids)
        self.assertEqual(failed_ids(wb), [ids[5]])
        self.assertEqual(written(wb, "Account"), 9)

    def test_line_groups_stay_in_one_call(self):
        sf = FakeSF(bad={f"00k{n:015d}" for n in range(3)})  # the three lines of BAD_OPP
        wb = WriteBack(sf, None, dry_run=False)
        groups = {BAD_OPP: [f"00k{n:015d}" for n in range(3)],
                  GOOD_OPP: [f"00k{n:015d}" for n in range(3, 5)]}
        for opp, lines in groups.items():
            for line_id in lines:
                wb.labels[("OpportunityLineItem", line_id)] = "Product"
                wb.counts[("OpportunityLineItem", "planned")] += 1
                wb.pending["OpportunityLineItem"].append(
                    (opp, {"attributes": {"type": "OpportunityLineItem"}, "Id": line_id, "Odoo_Qty_Delivered__c": 1}))
        wb._flush("OpportunityLineItem")
        self.assertEqual(sorted(failed_ids(wb)), sorted(groups[BAD_OPP]))
        self.assertEqual(written(wb, "OpportunityLineItem"), 2)
        # No call carries a partial opportunity: each call holds all or none of a group's lines.
        for call in sf.calls:
            for opp, lines in groups.items():
                present = [i for i in lines if i in call]
                self.assertIn(len(present), (0, len(lines)), f"group {opp} split across calls: {call}")

    def test_large_group_is_one_call_even_over_batch(self):
        big = [f"00k{n:015d}" for n in range(BATCH + 5)]
        sf = FakeSF()
        wb = WriteBack(sf, None, dry_run=False)
        for line_id in big:
            wb.labels[("OpportunityLineItem", line_id)] = "P"
            wb.counts[("OpportunityLineItem", "planned")] += 1
            wb.pending["OpportunityLineItem"].append(
                (BAD_OPP, {"attributes": {}, "Id": line_id, "Odoo_Qty_Delivered__c": 1}))
        wb._flush("OpportunityLineItem")
        self.assertEqual(len(sf.calls), 1)
        self.assertEqual(written(wb, "OpportunityLineItem"), len(big))


class ReportTests(unittest.TestCase):
    def test_opportunity_ids_parsed_once(self):
        found = _opportunity_ids([
            "field integrity exception: unknown (0064W00001EOyE7|1|95190|566244|16|620,...)",
            "x (0064W00001EOyE7|2|1), (0064W00001EOyE8ABC|2)",
        ])
        self.assertEqual(found, ["0064W00001EOyE7", "0064W00001EOyE8ABC"])

    def test_integrity_group_explained_once_with_opportunities(self):
        wb = WriteBack(None, None, dry_run=False)
        wb.labels[("Opportunity", "0064W00001EOyE7")] = "Acme renewal"
        msg = "field integrity exception: unknown (0064W00001EOyE7|1|95190|566244|16|620,...)"
        wb.failed = [
            {"sobject": "Opportunity", "id": "0064W00001EOyE7", "name": "Acme renewal",
             "code": INTEGRITY, "messages": [msg]},
            {"sobject": "OpportunityLineItem", "id": "00kAAAAAAAAAAAAAAA", "name": "Study",
             "code": INTEGRITY, "messages": [msg]},
        ]
        out = io.StringIO()
        with redirect_stdout(out):
            wb._report_failed()
        text = out.getvalue()
        self.assertEqual(text.count("opportunity totals don't match its products in Salesforce "
                                    "(data problem in this org)"), 1)
        self.assertIn("0064W00001EOyE7  Acme renewal", text)
        self.assertIn("OpportunityLineItem 00kAAAAAAAAAAAAAAA  Study", text)
        self.assertNotIn("None", text)


def _org_field(name, sf_type):
    return {"name": name, "type": sf_type, "updateable": True}


# The org's field names as describe returns them: Odoo_Id__c, not the Odoo_ID__c the connector was written with.
ORG_FIELDS = {
    "Account": [_org_field("Odoo_Id__c", "double"), _org_field("Odoo_Record__c", "url"),
                _org_field("Odoo_Sync_Error__c", "textarea"), _org_field("Odoo_Last_Sync__c", "datetime")],
    "Opportunity": [_org_field("Odoo_Id__c", "double"), _org_field("Odoo_Record__c", "url"),
                    _org_field("Odoo_Sync_Error__c", "textarea"), _org_field("Odoo_Last_Sync__c", "datetime")],
    "OpportunityLineItem": [_org_field("Odoo_Line_Id__c", "double"), _org_field("Odoo_Qty_Delivered__c", "double"),
                            _org_field("Odoo_Qty_Invoiced__c", "double"), _org_field("Odoo_Fulfilled__c", "boolean")],
}


class FakeOrg(FakeSF):
    """Describe and query answer as the org does: field names as the org stores them."""

    def __init__(self, rows=None, drop=(), **kwargs):
        super().__init__(**kwargs)
        self.session = SimpleNamespace()
        self.rows = dict(rows or {})
        self.drop = set(drop)
        self.describe_calls = []
        self.soql = []

    def describe(self, sobject):
        self.describe_calls.append(sobject)
        return {"fields": [f for f in ORG_FIELDS[sobject] if f["name"] not in self.drop]}

    def query(self, soql):
        self.soql.append(soql)
        ids = re.findall(r"'([^']+)'", soql)
        return [dict(self.rows[i]) for i in ids if i in self.rows]


class FieldNameTests(unittest.TestCase):
    """Field names are matched without regard to case, and the org's own names are used in SOQL and in the body."""

    def line_rows(self):
        return {
            "00k1": {"Id": "00k1", "Odoo_Line_Id__c": 938.0, "Odoo_Qty_Delivered__c": 0.0,
                     "Odoo_Qty_Invoiced__c": 0.0, "Odoo_Fulfilled__c": False},
            "00k2": {"Id": "00k2", "Odoo_Line_Id__c": None, "Odoo_Qty_Delivered__c": None,
                     "Odoo_Qty_Invoiced__c": None, "Odoo_Fulfilled__c": False},
        }

    def line_desired(self, line_id):
        return {"Odoo_Line_ID__c": 938 if line_id == "00k1" else 939, "Odoo_Qty_Delivered__c": 0,
                "Odoo_Qty_Invoiced__c": 0, "Odoo_Fulfilled__c": False}

    def test_soql_uses_the_org_name_and_results_come_back_under_the_configured_name(self):
        sf = FakeOrg(rows=self.line_rows())
        wb = WriteBack(sf, None, dry_run=True)
        current = wb._current("OpportunityLineItem", ["00k1"])
        self.assertIn("Odoo_Line_Id__c", sf.soql[0])
        self.assertNotIn("Odoo_Line_ID__c", sf.soql[0])
        self.assertEqual(current["00k1"]["Odoo_Line_ID__c"], 938.0)

    def test_record_with_an_id_already_in_the_org_is_not_planned(self):
        sf = FakeOrg(rows=self.line_rows())
        wb = WriteBack(sf, None, dry_run=True)
        current = wb._current("OpportunityLineItem", ["00k1"])
        wb._plan("OpportunityLineItem", "00k1", "", current["00k1"], self.line_desired("00k1"),
                 group="006A")
        self.assertEqual(wb.counts[("OpportunityLineItem", "unchanged")], 1)
        self.assertEqual(wb.counts[("OpportunityLineItem", "planned")], 0)
        self.assertEqual(wb.pending, {})

    def test_only_the_real_gaps_are_planned(self):
        sf = FakeOrg(rows=self.line_rows())
        wb = WriteBack(sf, None, dry_run=True)
        current = wb._current("OpportunityLineItem", ["00k1", "00k2"])
        for line_id in ("00k1", "00k2"):
            wb._plan("OpportunityLineItem", line_id, "", current[line_id], self.line_desired(line_id), group="006A")
        self.assertEqual(wb.counts[("OpportunityLineItem", "unchanged")], 1)
        self.assertEqual(wb.counts[("OpportunityLineItem", "planned")], 1)

    def test_patch_body_uses_the_org_name(self):
        sf = FakeOrg(rows=self.line_rows())
        wb = WriteBack(sf, None, dry_run=False)
        current = wb._current("OpportunityLineItem", ["00k2"])
        wb._plan("OpportunityLineItem", "00k2", "", current["00k2"], self.line_desired("00k2"), group="006A")
        payload = wb.pending["OpportunityLineItem"][0][1]
        self.assertIn("Odoo_Line_Id__c", payload)
        self.assertNotIn("Odoo_Line_ID__c", payload)
        self.assertEqual(payload["Odoo_Line_Id__c"], 939)

    def test_account_and_opportunity_ids_use_the_org_name(self):
        sf = FakeOrg(rows={"006A": {"Id": "006A", "Odoo_Id__c": None, "Odoo_Record__c": None,
                                    "Odoo_Sync_Error__c": None, "Odoo_Last_Sync__c": None}})
        wb = WriteBack(sf, None, dry_run=False)
        current = wb._current("Opportunity", ["006A"])
        wb._plan("Opportunity", "006A", "Acme", current["006A"],
                 {"Odoo_ID__c": 48, "Odoo_Record__c": "http://x/48"}, stamp=True)
        payload = wb.pending["Opportunity"][0][1]
        self.assertEqual(payload["Odoo_Id__c"], 48)
        self.assertIn("Odoo_Last_Sync__c", payload)
        self.assertNotIn("Odoo_ID__c", payload)

    def test_missing_field_is_reported_not_guessed(self):
        sf = FakeOrg(drop={"Odoo_Qty_Invoiced__c"})
        wb = WriteBack(sf, None, dry_run=True)
        self.assertFalse(wb._fields_ok("OpportunityLineItem"))
        out = io.StringIO()
        with redirect_stdout(out):
            wb._fields_ok("OpportunityLineItem")  # cached: no second message
        self.assertEqual(out.getvalue(), "")

    def test_describe_runs_once_per_run_across_write_backs(self):
        sf = FakeOrg(rows=self.line_rows())
        for _ in range(3):
            WriteBack(sf, None, dry_run=True)._fields_ok("OpportunityLineItem")
        self.assertEqual(sf.describe_calls.count("OpportunityLineItem"), 1)
        self.assertEqual(_field_map(sf, "OpportunityLineItem")["odoo_line_id__c"]["name"], "Odoo_Line_Id__c")

    def test_case_difference_is_announced_once_per_run(self):
        sf = FakeOrg(rows=self.line_rows())
        out = io.StringIO()
        with redirect_stdout(out):
            for _ in range(3):
                WriteBack(sf, None, dry_run=True)._fields_ok("OpportunityLineItem")
        self.assertEqual(out.getvalue().count("Odoo_Line_ID__c is Odoo_Line_Id__c in this org"), 1)


if __name__ == "__main__":
    unittest.main()
