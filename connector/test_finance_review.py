"""Tests for the Finance review rules: unconfirmed quotations are cancelled and reopened (§4.6), and products
are matched by Salesforce Id first (§3). No Odoo or Salesforce calls: FakeOdoo keeps tables in memory and
records every write and call.

Run from the repo root:  python -m unittest connector.test_finance_review
"""
import unittest
from unittest import mock

from connector import config
from connector.sync_lines import LineSync
from connector.sync_orders import OrderSync, QUOTE_CANCELLED, STAGE_AWARD_WON

OPP_ID = "006AAAAAAAAAAAAAAA"
OPP_NAME = "Acme renewal"
STAGE_07 = STAGE_AWARD_WON
STAGE_06 = "06: Proposal"


def _match(row, domain):
    for field, op, value in domain:
        have = row.get(field)
        if op == "=":
            ok = have == value
        elif op == "!=":
            ok = have != value
        elif op == "in":
            ok = have in value
        elif op == ">":
            ok = have is not None and have > value
        elif op == "ilike":
            ok = value.lower() in str(have or "").lower()
        else:
            raise ValueError(f"unsupported operator {op}")
        if not ok:
            return False
    return True


WIZARD = {"type": "ir.actions.act_window", "res_model": "sale.order.cancel", "target": "new"}


class FakeOdoo:
    def __init__(self, tables=None, no_effect=()):
        self.tables = {model: [dict(r) for r in rows] for model, rows in (tables or {}).items()}
        self.log = []  # ("call", model, method, kwargs) / ("write", model, ids, vals) / ("create", model, vals)
        self._next_id = 1000
        self.no_effect = set(no_effect)  # methods that run but leave the order's state as it was

    def search_read(self, model, domain, fields, **kwargs):
        return [dict(r) for r in self.tables.get(model, []) if _match(r, domain)]

    def call(self, model, method, **kwargs):
        self.log.append(("call", model, method, kwargs))
        if method in self.no_effect:
            # As Odoo can do: action_cancel on a sent quotation returns the cancel wizard and cancels nothing.
            return WIZARD if method == "action_cancel" else None
        new_state = {"action_cancel": "cancel", "action_draft": "draft"}.get(method)
        if new_state:
            for row in self.tables.get(model, []):
                if row.get("id") in kwargs.get("ids", []):
                    row["state"] = new_state
        return None

    def write(self, model, ids, vals):
        self.log.append(("write", model, list(ids), dict(vals)))
        for row in self.tables.get(model, []):
            if row.get("id") in ids:
                row.update(vals)

    def create(self, model, vals_list):
        ids = []
        for vals in vals_list:
            self.log.append(("create", model, dict(vals)))
            self._next_id += 1
            row = dict(vals, id=self._next_id)
            if model == "product.product":
                # Odoo delegates the product.template fields to a template created with the variant.
                tmpl = self._next_id + 5000
                self.tables.setdefault("product.template", []).append(
                    {"id": tmpl, "sf_product_id": vals.get("sf_product_id") or None})
                row["product_tmpl_id"] = [tmpl, vals.get("name")]
            self.tables.setdefault(model, []).append(row)
            ids.append(self._next_id)
        return ids

    def calls(self, method):
        return [entry for entry in self.log if entry[0] == "call" and entry[2] == method]

    def writes(self, model):
        return [entry for entry in self.log if entry[0] == "write" and entry[1] == model]

    def bodies(self):
        return [entry[3]["body"] for entry in self.calls("message_post")]


def make_order_sync(odoo, dry_run=False):
    sync = OrderSync(odoo, dry_run)
    sync.lines = LineSync(sync)
    sync.lines.load()
    return sync


def order_row(state, sf_stage=STAGE_07, cancelled=False):
    return {"id": 1, "sf_opportunity_id": OPP_ID, "sf_stage": sf_stage, "state": state,
            "sf_cancelled_by_sync": cancelled, "project_id": False, "activity_ids": []}


def opp(stage):
    return {"Id": OPP_ID, "Name": OPP_NAME, "StageName": stage, "AccountId": None}


BASE_TABLES = {
    "sale.order": [],
    "mail.message": [],
    "res.users": [{"id": 9, "login": "finance@example.com", "active": True, "share": False}],
    "product.template": [],
    "product.product": [],
    "sale.order.line": [],
}


class StageReversalTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(config, "FINANCE_ACTIVITY_USER", "finance@example.com")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_reversal(self, odoo, existing, stage):
        sync = make_order_sync(odoo)
        sync.stage_reversal(opp(stage), existing)
        return sync

    def test_draft_quotation_is_cancelled_with_no_todo(self):
        odoo = FakeOdoo({**BASE_TABLES, "sale.order": [order_row("draft")]})
        existing = odoo.tables["sale.order"][0]
        self.run_reversal(odoo, existing, STAGE_06)
        self.assertEqual(len(odoo.calls("action_cancel")), 1)
        self.assertEqual(odoo.calls("activity_schedule"), [])
        self.assertTrue(any(QUOTE_CANCELLED in body for body in odoo.bodies()))
        row = odoo.tables["sale.order"][0]
        self.assertEqual(row["state"], "cancel")
        self.assertTrue(row["sf_cancelled_by_sync"])
        self.assertEqual(row["sf_stage"], STAGE_06)

    def test_flag_is_written_before_the_cancel(self):
        # If the run stops between the two, the next run must still see the quotation as the connector's.
        odoo = FakeOdoo({**BASE_TABLES, "sale.order": [order_row("sent")]})
        self.run_reversal(odoo, odoo.tables["sale.order"][0], STAGE_06)
        kinds = [(e[0], e[2] if e[0] == "call" else e[3]) for e in odoo.log]
        flag_at = next(i for i, e in enumerate(odoo.log) if e[0] == "write" and e[3].get("sf_cancelled_by_sync"))
        cancel_at = next(i for i, e in enumerate(odoo.log) if e[0] == "call" and e[2] == "action_cancel")
        self.assertLess(flag_at, cancel_at, kinds)

    def test_confirmed_order_keeps_todo_and_is_not_cancelled(self):
        odoo = FakeOdoo({**BASE_TABLES, "sale.order": [order_row("sale")]})
        self.run_reversal(odoo, odoo.tables["sale.order"][0], STAGE_06)
        self.assertEqual(odoo.calls("action_cancel"), [])
        self.assertEqual(len(odoo.calls("activity_schedule")), 1)
        self.assertTrue(any("The order was not changed" in body for body in odoo.bodies()))
        self.assertEqual(odoo.tables["sale.order"][0]["state"], "sale")

    def test_quotation_cancelled_by_hand_gets_todo_not_reopened_later(self):
        odoo = FakeOdoo({**BASE_TABLES, "sale.order": [order_row("cancel", cancelled=False)]})
        self.run_reversal(odoo, odoo.tables["sale.order"][0], STAGE_06)
        self.assertEqual(odoo.calls("action_cancel"), [])
        self.assertEqual(len(odoo.calls("activity_schedule")), 1)

    def test_repeat_run_after_partial_failure_does_not_cancel_or_note_again(self):
        # Cancel and flag done, sf_stage not yet written, note already posted.
        note = f"Salesforce stage changed from {STAGE_07} to {STAGE_06}. {QUOTE_CANCELLED}, because"
        odoo = FakeOdoo({**BASE_TABLES,
                         "sale.order": [order_row("cancel", cancelled=True)],
                         "mail.message": [{"id": 5, "model": "sale.order", "res_id": 1, "body": note}]})
        self.run_reversal(odoo, odoo.tables["sale.order"][0], STAGE_06)
        self.assertEqual(odoo.calls("action_cancel"), [])
        self.assertEqual(odoo.bodies(), [])
        self.assertEqual(odoo.tables["sale.order"][0]["sf_stage"], STAGE_06)


class ReopenTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(config, "FINANCE_ACTIVITY_USER", "finance@example.com")
        patcher.start()
        self.addCleanup(patcher.stop)

    def sync_with(self, existing_row):
        odoo = FakeOdoo({**BASE_TABLES, "sale.order": [existing_row]})
        sync = make_order_sync(odoo)
        headers = []
        sync.sync_header = lambda o, existing: (headers.append(dict(existing)) or (True, existing["id"]))
        sync.lines.sync = lambda *args, **kwargs: None
        return sync, odoo, headers

    def test_connector_cancelled_quotation_is_reopened_on_return(self):
        sync, odoo, headers = self.sync_with(order_row("cancel", sf_stage=STAGE_06, cancelled=True))
        sync.create_or_update(opp(STAGE_07), odoo.tables["sale.order"][0])
        self.assertEqual(len(odoo.calls("action_draft")), 1)
        self.assertEqual(odoo.calls("action_cancel"), [])
        self.assertFalse(odoo.tables["sale.order"][0]["sf_cancelled_by_sync"])
        self.assertEqual(headers[0]["state"], "draft")  # the header is synced on the reopened quotation
        self.assertTrue(any("was reopened" in body for body in odoo.bodies()))

    def test_closed_also_reopens(self):
        sync, odoo, _ = self.sync_with(order_row("cancel", sf_stage=STAGE_06, cancelled=True))
        sync.create_or_update(opp("Closed"), odoo.tables["sale.order"][0])
        self.assertEqual(len(odoo.calls("action_draft")), 1)

    def test_quotation_cancelled_by_hand_is_never_reopened(self):
        sync, odoo, headers = self.sync_with(order_row("cancel", sf_stage=STAGE_06, cancelled=False))
        sync.create_or_update(opp(STAGE_07), odoo.tables["sale.order"][0])
        self.assertEqual(odoo.calls("action_draft"), [])
        self.assertEqual(headers[0]["state"], "cancel")
        self.assertFalse(any("was reopened" in body for body in odoo.bodies()))

    def test_reopen_is_not_repeated_after_partial_failure(self):
        # action_draft done, flag not yet cleared: the next run reopens once more without calling draft again.
        sync, odoo, _ = self.sync_with(order_row("draft", sf_stage=STAGE_06, cancelled=True))
        sync.create_or_update(opp(STAGE_07), odoo.tables["sale.order"][0])
        self.assertEqual(odoo.calls("action_draft"), [])
        self.assertFalse(odoo.tables["sale.order"][0]["sf_cancelled_by_sync"])


class ProductMatchTests(unittest.TestCase):
    def tables(self, template_sf_id=None, name="Study A", code="CODE1", linked_sf=None):
        return {**BASE_TABLES,
                "product.template": [{"id": 10, "sf_product_id": template_sf_id}],
                "product.product": [{"id": 100, "default_code": code, "name": name, "product_tmpl_id": [10, name]}]}

    def line(self, sf_product_id, code="CODE1", name="Study A"):
        return {"Id": "02iLINE", "Product2Id": sf_product_id,
                "Product2": {"ProductCode": code, "Name": name}}

    def resolve(self, odoo, sf_line, auto=False):
        sync = make_order_sync(odoo)
        problems = {}
        with mock.patch.object(config, "AUTO_CREATE_PRODUCTS", auto):
            pid = sync.lines.resolve_product(sf_line, problems)
        return sync, pid, problems

    def test_match_by_id_writes_nothing(self):
        odoo = FakeOdoo(self.tables(template_sf_id="01tA"))
        sync, pid, _ = self.resolve(odoo, self.line("01tA"))
        self.assertEqual(pid, 100)
        self.assertEqual(odoo.log, [])
        self.assertEqual(sync.counts["products_matched_id"], 1)

    def test_name_or_code_fallback_stamps_the_id_once(self):
        odoo = FakeOdoo(self.tables(template_sf_id=None))
        sync, pid, _ = self.resolve(odoo, self.line("01tA"))
        self.assertEqual(pid, 100)
        self.assertEqual(odoo.writes("product.template"),
                         [("write", "product.template", [10], {"sf_product_id": "01tA"})])
        self.assertEqual(sync.counts["products_matched_name"], 1)
        self.assertEqual(sync.counts["products_stamped"], 1)

    def test_next_run_matches_the_stamped_product_by_id(self):
        odoo = FakeOdoo(self.tables(template_sf_id=None))
        self.resolve(odoo, self.line("01tA"))  # first run stamps
        odoo.tables["product.template"][0]["sf_product_id"] = "01tA"  # as the write left it
        sync, pid, _ = self.resolve(odoo, self.line("01tA"))
        self.assertEqual(pid, 100)
        self.assertEqual(sync.counts["products_matched_id"], 1)
        self.assertEqual(sync.counts["products_matched_name"], 0)
        self.assertEqual(len(odoo.writes("product.template")), 1)  # only the first run wrote

    def test_product_linked_to_another_salesforce_product_is_not_used(self):
        odoo = FakeOdoo(self.tables(template_sf_id="01tOTHER"))
        sync, pid, problems = self.resolve(odoo, self.line("01tB"))
        self.assertFalse(pid)
        self.assertIn("product not in Odoo", problems["product_id"])
        self.assertIn("linked to another Salesforce product", problems["product_id"])
        self.assertEqual(odoo.writes("product.template"), [])
        self.assertEqual(sync.counts["products_missing"], 1)

    def test_missing_product_names_it_when_auto_create_is_off(self):
        odoo = FakeOdoo({**BASE_TABLES})
        sync = make_order_sync(odoo)
        with mock.patch.object(config, "AUTO_CREATE_PRODUCTS", False):
            messages = sync.lines.unknown_products([self.line("01tZ", code="Z1", name="Study Z")])
        self.assertEqual(messages, ["product not in Odoo: Study Z, code Z1, Salesforce 01tZ"])
        self.assertEqual([e for e in odoo.log if e[0] == "create"], [])

    def test_auto_create_stamps_the_id_on_the_new_product(self):
        odoo = FakeOdoo({**BASE_TABLES})
        sync = make_order_sync(odoo)
        with mock.patch.object(config, "AUTO_CREATE_PRODUCTS", True):
            messages = sync.lines.unknown_products([self.line("01tZ", code="Z1", name="Study Z")])
        self.assertEqual(messages, [])
        creates = [e for e in odoo.log if e[0] == "create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][2]["sf_product_id"], "01tZ")
        self.assertEqual(sync.counts["products_created"], 1)

    def test_order_with_missing_product_is_not_written(self):
        odoo = FakeOdoo({**BASE_TABLES})
        sync = make_order_sync(odoo)
        headers = []
        sync.sync_header = lambda o, existing: (headers.append(o) or (True, 1))
        sync.sf_lines = {OPP_ID: [self.line("01tZ", code="Z1", name="Study Z")]}
        with mock.patch.object(config, "AUTO_CREATE_PRODUCTS", False):
            sync.create_or_update(opp(STAGE_07), None)
        self.assertEqual(headers, [])
        self.assertEqual(odoo.log, [])
        self.assertIn("order not synced: product not in Odoo: Study Z, code Z1, Salesforce 01tZ",
                      sync.issues[OPP_ID])


class CancelVerificationTests(unittest.TestCase):
    """Odoo must confirm the state: a wizard from action_cancel, or an action_draft that does not take effect,
    is not counted as done."""

    def setUp(self):
        patcher = mock.patch.object(config, "FINANCE_ACTIVITY_USER", "finance@example.com")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_wizard_returned_instead_of_cancel_is_not_counted(self):
        odoo = FakeOdoo({**BASE_TABLES, "sale.order": [order_row("sent")]}, no_effect={"action_cancel"})
        sync = make_order_sync(odoo)
        row = odoo.tables["sale.order"][0]
        sync.stage_reversal(opp(STAGE_06), row)
        self.assertEqual(row["state"], "sent")
        self.assertFalse(row["sf_cancelled_by_sync"])  # flag cleared again
        self.assertEqual(sync.counts["quotations_cancelled"], 0)
        self.assertFalse(any(QUOTE_CANCELLED in body for body in odoo.bodies()))
        self.assertEqual(len(odoo.calls("activity_schedule")), 1)  # fell back to note + To Do
        self.assertTrue(any("could not cancel quotation" in m for m in sync.issues[OPP_ID]))
        self.assertEqual(row["sf_stage"], STAGE_06)  # the reversal itself is still recorded

    def test_cancel_is_called_with_disable_cancel_warning(self):
        odoo = FakeOdoo({**BASE_TABLES, "sale.order": [order_row("sent")]})
        make_order_sync(odoo).stage_reversal(opp(STAGE_06), odoo.tables["sale.order"][0])
        cancels = odoo.calls("action_cancel")
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0][3]["context"], {"disable_cancel_warning": True})

    def test_unconfirmed_cancel_is_not_retried_automatically(self):
        # The reversal is recorded (sf_stage = 06), so the next run sees no change and does not try again.
        # Finance gets the To Do, and that is the handoff. A second To Do is not created either.
        odoo = FakeOdoo({**BASE_TABLES, "sale.order": [order_row("sent")]}, no_effect={"action_cancel"})
        row = odoo.tables["sale.order"][0]
        make_order_sync(odoo).stage_reversal(opp(STAGE_06), row)
        second = make_order_sync(odoo)
        second.stage_reversal(opp(STAGE_06), row)
        self.assertEqual(len(odoo.calls("action_cancel")), 1)
        self.assertEqual(len(odoo.calls("activity_schedule")), 1)
        self.assertEqual(second.counts["reversed_unchanged"], 1)

    def test_action_draft_not_taking_effect_leaves_the_order_alone(self):
        odoo = FakeOdoo({**BASE_TABLES, "sale.order": [order_row("cancel", sf_stage=STAGE_06, cancelled=True)]},
                        no_effect={"action_draft"})
        sync = make_order_sync(odoo)
        headers = []
        sync.sync_header = lambda o, existing: (headers.append(dict(existing)) or (True, existing["id"]))
        sync.lines.sync = lambda *args, **kwargs: None
        row = odoo.tables["sale.order"][0]
        sync.create_or_update(opp(STAGE_07), row)
        self.assertEqual(headers, [])  # header and lines not synced
        self.assertEqual(row["state"], "cancel")
        self.assertTrue(row["sf_cancelled_by_sync"])  # flag kept, so the next run tries again
        self.assertEqual(odoo.writes("sale.order"), [])  # no flag clear, no stage update
        self.assertFalse(any("was reopened" in body for body in odoo.bodies()))
        self.assertTrue(any("could not reopen quotation" in m for m in sync.issues[OPP_ID]))


if __name__ == "__main__":
    unittest.main()
