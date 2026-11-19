"""Print, Send and the saved invoice PDF for customer invoices and credit notes: the partner's own template wins, otherwise
the company's layout; several invoices render each with their company's report and come back in the order asked for.
The modules are loaded against a stand-in for odoo (base classes and the environment are simple fakes). No Odoo needed.

Run from the repo root:  python -m unittest connector.test_invoice_send_and_print
"""
import importlib.util
import sys
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "odoo_addons" / "sf_connector" / "models"


def load(name, filename, base):
    """Load one model file with models.Model / AbstractModel bound to the given fake base class."""
    odoo = types.ModuleType("odoo")
    odoo.models = types.SimpleNamespace(Model=base, AbstractModel=base)
    odoo.api = types.SimpleNamespace(model=lambda f: f)
    with mock.patch.dict(sys.modules, {"odoo": odoo}):
        spec = importlib.util.spec_from_file_location(name, MODELS / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class Company:
    def __init__(self, cid, layout):
        self.id = cid
        self.sf_invoice_layout = layout


class Move:
    def __init__(self, mid, move_type="out_invoice", company=None, partner_choice=None):
        self.id = mid
        self.move_type = move_type
        self.company_id = company
        self.commercial_partner_id = types.SimpleNamespace(
            with_company=lambda c: types.SimpleNamespace(invoice_template_pdf_report_id=partner_choice))

    def _sf_invoice_report_xmlid(self):
        return ("sf_connector.report_sf_invoice_gamma" if self.company_id.sf_invoice_layout == "gamma"
                else "sf_connector.report_sf_invoice_llc")


class Report:
    def __init__(self, name, report_name="sf_connector.report_sf_invoice", model="account.move"):
        self.name = name
        self.report_name = report_name
        self.model = model


class FakeEnv(dict):
    """ref(xmlid) gives a report stand-in; env['account.move'].browse gives the moves by id."""
    def __init__(self, moves):
        by_id = {m.id: m for m in moves}
        super().__init__({"account.move": types.SimpleNamespace(browse=lambda ids: [by_id[i] for i in ids])})
        self.refs = {}

    def ref(self, xmlid):
        return self.refs.setdefault(xmlid, Report(xmlid, report_name="sf_connector.report_sf_invoice"))


class RenderBase:
    """The Odoo renderer, as far as these tests need it: one stream per id, pdf."""
    def __init__(self, env, report):
        self.env = env
        self.report = report
        self.calls = []

    def with_company(self, company_id):
        return self

    def _get_report(self, report_ref):
        return report_ref if isinstance(report_ref, Report) else self.report

    def _pre_render_qweb_pdf(self, report_ref, res_ids=None, data=None):
        self.calls.append((getattr(report_ref, "name", report_ref), list(res_ids)))
        return OrderedDict((i, {"stream": f"pdf-{i}"}) for i in res_ids), "pdf"


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.llc, self.gamma = Company(1, "llc"), Company(2, "gamma")

    def render(self, moves, report, res_ids):
        module = load("ir_actions_report_under_test", "ir_actions_report.py", RenderBase)
        instance = module.IrActionsReport(FakeEnv(moves), report)  # the Odoo renderer is the base class
        result = instance._pre_render_qweb_pdf(report, res_ids=res_ids)
        return result, instance.calls

    def test_mixed_companies_each_render_with_their_own_report_and_keep_the_order(self):
        moves = [Move(1, company=self.llc), Move(2, company=self.gamma), Move(3, company=self.llc)]
        (streams, kind), calls = self.render(moves, Report("standard", report_name="account.report_invoice_with_payments",
                                                          model="account.move"), [3, 1, 2])
        self.assertEqual(kind, "pdf")
        self.assertEqual(list(streams.keys()), [3, 1, 2])  # the order asked for
        names = sorted(name for name, _ in calls)
        self.assertEqual(names, ["sf_connector.report_sf_invoice_gamma", "sf_connector.report_sf_invoice_llc"])
        self.assertIn(("sf_connector.report_sf_invoice_llc", [3, 1]), calls)  # in the order asked for
        self.assertIn(("sf_connector.report_sf_invoice_gamma", [2]), calls)

    def test_our_own_report_is_split_by_company_too(self):
        moves = [Move(1, company=self.gamma), Move(2, company=self.llc)]
        (streams, _), calls = self.render(moves, Report("ours"), [1, 2])
        self.assertEqual(sorted(streams.keys()), [1, 2])
        self.assertEqual(len(calls), 2)

    def test_bills_and_other_documents_are_left_to_odoo(self):
        moves = [Move(1, move_type="in_invoice", company=self.llc)]
        (streams, kind), calls = self.render(moves, Report("standard", report_name="account.report_invoice_with_payments",
                                                          model="account.move"), [1])
        self.assertEqual(calls, [("standard", [1])])  # rendered once, with the report asked for

    def test_other_reports_are_left_to_odoo(self):
        moves = [Move(1, company=self.llc)]
        (streams, kind), calls = self.render(moves, Report("payment receipt", report_name="account.report_payment_receipt",
                                                          model="account.payment"), [1])
        self.assertEqual(calls, [("payment receipt", [1])])


class DefaultReportTests(unittest.TestCase):
    """Account send's default PDF report, as Print and the wizard use it."""

    def setUp(self):
        class Base:
            def __init__(self, env):
                self.env = env

            def _get_default_pdf_report_id(self, move):
                return "STANDARD"
        self.Base = Base
        self.module = load("account_move_send_under_test", "account_move_send.py", Base)
        self.company = Company(2, "gamma")

    def env(self):
        return types.SimpleNamespace(ref=lambda xmlid: f"OURS:{xmlid}")

    def send(self, move):
        return self.module.AccountMoveSend(self.env())._get_default_pdf_report_id(move)

    def test_customer_invoice_gets_its_company_layout(self):
        self.assertEqual(self.send(Move(1, company=self.company)), "OURS:sf_connector.report_sf_invoice_gamma")

    def test_credit_note_gets_its_company_layout(self):
        self.assertEqual(self.send(Move(2, move_type="out_refund", company=Company(1, "llc"))),
                         "OURS:sf_connector.report_sf_invoice_llc")

    def test_partner_explicit_choice_wins(self):
        self.assertEqual(self.send(Move(3, company=self.company, partner_choice="PARTNER")), "PARTNER")

    def test_bills_keep_odoos_default(self):
        self.assertEqual(self.send(Move(4, move_type="in_invoice", company=self.company)), "STANDARD")


if __name__ == "__main__":
    unittest.main()
