"""Print, Send and the saved invoice PDF for customer invoices and credit notes: always the company's own layout. A
partner's template is ignored, Odoo's standard layouts are never used, and the Send wizard does not offer them.
Several invoices render each with their company's report and come back in the order asked for.
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
    odoo.fields = _Anything()
    exceptions = types.ModuleType("odoo.exceptions")
    exceptions.UserError = type("UserError", (Exception,), {})
    odoo.exceptions = exceptions
    with mock.patch.dict(sys.modules, {"odoo": odoo, "odoo.exceptions": exceptions}):
        spec = importlib.util.spec_from_file_location(name, MODELS / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class _Anything:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


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

    def test_standard_pdf_without_payment_is_rendered_with_ours_too(self):
        moves = [Move(1, company=self.llc)]
        (streams, kind), calls = self.render(moves, Report("PDF without Payment", report_name="account.report_invoice",
                                                          model="account.move"), [1])
        self.assertEqual(calls, [("sf_connector.report_sf_invoice_llc", [1])])

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

    def test_partner_template_is_ignored_for_customer_documents(self):
        self.assertEqual(self.send(Move(3, company=self.company, partner_choice="PARTNER")),
                         "OURS:sf_connector.report_sf_invoice_gamma")

    def test_partner_standard_layout_is_ignored_too(self):
        self.assertEqual(self.send(Move(5, company=Company(1, "llc"), partner_choice="Invoice PDF")),
                         "OURS:sf_connector.report_sf_invoice_llc")

    def test_bills_keep_odoos_default(self):
        self.assertEqual(self.send(Move(4, move_type="in_invoice", company=self.company)), "STANDARD")


class WizardChoiceTests(unittest.TestCase):
    """The Send wizard's choices: Odoo's standard layouts are removed for customer documents only."""

    class Reports(list):
        def filtered(self, function):
            return WizardChoiceTests.Reports([r for r in self if function(r)])

    def setUp(self):
        self.module = load("account_move_under_test", "account_move.py", object)

    def choices(self, moves):
        reports = self.Reports([Report("Invoice PDF", "account.report_invoice_with_payments"),
                                Report("PDF without Payment", "account.report_invoice"),
                                Report("Original Bills", "account.report_original_vendor_bill")])
        return [r.report_name for r in self.module.sf_without_standard_reports(reports, moves)]

    def test_customer_documents_get_no_standard_layout(self):
        self.assertEqual(self.choices([Move(1)]), ["account.report_original_vendor_bill"])
        self.assertEqual(self.choices([Move(1, move_type="out_refund")]), ["account.report_original_vendor_bill"])

    def test_bills_keep_odoos_choices(self):
        self.assertEqual(len(self.choices([Move(1, move_type="in_invoice")])), 3)

    def test_mixed_selection_keeps_odoos_choices(self):
        self.assertEqual(len(self.choices([Move(1), Move(2, move_type="in_invoice")])), 3)


if __name__ == "__main__":
    unittest.main()
