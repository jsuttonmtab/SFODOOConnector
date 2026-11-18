"""Order company and pricelist rules. A pricelist is taken from the billing company only (never a shared one, never
created). A blank or unmatched billing entity, or a company with no pricelist in the currency, refuses the order. A
company or pricelist that differs from an existing order stops that order: no header write, no lines synced.
No Odoo or Salesforce calls: FakeOdoo keeps tables in memory.

Run from the repo root:  python -m unittest connector.test_order_company
"""
import unittest

from connector.test_finance_review import FakeOdoo, make_order_sync

OPP = "006AAAAAAAAAAAAAAA"
GAMMA, MTAB = 2, 1
TABLES = {
    "res.company": [{"id": MTAB, "name": "mTab LLC"}, {"id": GAMMA, "name": "G.A.M.M.A"}],
    "res.currency": [{"id": 144, "name": "GBP", "active": True}],
    "product.pricelist": [
        {"id": 1, "name": "Default", "currency_id": [144, "GBP"], "company_id": [GAMMA, "G.A.M.M.A"], "active": True},
        {"id": 5, "name": "SF GBP", "currency_id": [144, "GBP"], "company_id": False, "active": True},
    ],
}
OPP_ROW = {"Id": OPP, "Name": "Acme renewal", "CurrencyIsoCode": "GBP"}


class OrderPricelistTests(unittest.TestCase):
    def setUp(self):
        self.odoo = FakeOdoo(TABLES)
        self.sync = make_order_sync(self.odoo, dry_run=False)
        self.sync.load_reference()

    def test_company_gets_its_own_pricelist_in_the_currency(self):
        self.assertEqual(self.sync.order_pricelist("GBP", GAMMA), 1)

    def test_shared_pricelist_is_never_used_for_a_company(self):
        self.odoo.tables["product.pricelist"] = [TABLES["product.pricelist"][1]]  # only the shared SF GBP
        self.sync = make_order_sync(self.odoo, dry_run=False)
        self.sync.load_reference()
        self.assertIsNone(self.sync.order_pricelist("GBP", MTAB))
        self.assertIsNone(self.sync.order_pricelist("GBP", GAMMA))

    def test_no_pricelist_is_created_when_the_company_has_none(self):
        self.odoo.tables["product.pricelist"] = []
        self.sync = make_order_sync(self.odoo, dry_run=False)
        self.sync.load_reference()
        self.assertIsNone(self.sync.order_pricelist("GBP", GAMMA))
        self.assertFalse([c for c in self.odoo.log if c[0] == "create"])


class OrderHeaderStopTests(unittest.TestCase):
    def setUp(self):
        self.odoo = FakeOdoo(dict(TABLES, **{"sale.order": []}))
        self.sync = make_order_sync(self.odoo, dry_run=False)

    def stub_vals(self, vals, problems):
        self.sync.order_vals = lambda opp, partner_id: (dict(vals), dict(problems))

    def test_blank_billing_entity_refuses_a_new_order(self):
        self.stub_vals({"name": "x"}, {"company_id": "billing entity not set"})
        ok, order_id = self.sync.sync_header(dict(OPP_ROW), None)
        self.assertFalse(ok)
        self.assertIsNone(order_id)
        self.assertFalse([c for c in self.odoo.log if c[0] == "create"])
        self.assertIn("billing entity not set", self.sync.issues[OPP][-1])

    def test_missing_pricelist_refuses_a_new_order_and_says_why(self):
        self.stub_vals({"company_id": GAMMA}, {"pricelist_id": "G.A.M.M.A has no GBP pricelist in Odoo"})
        ok, _ = self.sync.sync_header(dict(OPP_ROW), None)
        self.assertFalse(ok)
        self.assertFalse([c for c in self.odoo.log if c[0] == "create"])
        self.assertIn("no GBP pricelist", self.sync.issues[OPP][-1])

    def test_unmatched_billing_entity_leaves_an_existing_order_and_its_lines_alone(self):
        existing = {"id": 7, "sf_opportunity_id": OPP, "company_id": [MTAB, "mTab LLC"], "pricelist_id": [5, "SF GBP"]}
        self.stub_vals({"note": "new"}, {"company_id": "billing entity 'Nowhere' not resolved"})
        ok, order_id = self.sync.sync_header(dict(OPP_ROW), existing)
        self.assertFalse(ok)
        self.assertEqual(order_id, 7)
        self.assertFalse([c for c in self.odoo.log if c[0] in ("write", "create")])

    def test_company_mismatch_on_an_existing_order_stops_it_with_an_error(self):
        existing = {"id": 7, "sf_opportunity_id": OPP, "company_id": [MTAB, "mTab LLC"], "pricelist_id": [5, "SF GBP"]}
        self.stub_vals({"company_id": GAMMA, "pricelist_id": 1, "note": "new"}, {})
        ok, order_id = self.sync.sync_header(dict(OPP_ROW), existing)
        self.assertFalse(ok)
        self.assertEqual(order_id, 7)
        self.assertFalse([c for c in self.odoo.log if c[0] in ("write", "create")], "nothing is written")
        message = self.sync.issues[OPP][-1]
        self.assertEqual(len(self.sync.issues[OPP]), 2)  # company and pricelist both differ
        self.assertIn("locked field company_id changed 1 -> 2", self.sync.issues[OPP][0])
        self.assertIn("order not synced", message)

    def test_matching_company_and_pricelist_do_not_stop_the_order(self):
        existing = {"id": 7, "sf_opportunity_id": OPP, "company_id": [GAMMA, "G.A.M.M.A"],
                    "pricelist_id": [1, "Default"], "sf_stage": "07: Award Won / Contract signed",
                    "partner_id": [3, "Acme"], "project_id": [9, "DS"]}
        self.stub_vals({"company_id": GAMMA, "pricelist_id": 1, "note": "new"}, {})
        ok, order_id = self.sync.sync_header(dict(OPP_ROW), existing)
        self.assertTrue(ok)
        self.assertEqual(order_id, 7)


if __name__ == "__main__":
    unittest.main()
