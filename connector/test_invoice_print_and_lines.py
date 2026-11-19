"""Invoice titles, the early-payment box on credit notes, the description copy on invoice lines (refreshed when a draft
is posted, filled for existing invoices by the 19.0.1.8.12 migration), and the Item Description column on the invoice
form. The model and the migration are loaded against a stand-in for the odoo package; no Odoo is needed.

Run from the repo root:  python -m unittest connector.test_invoice_print_and_lines
"""
import importlib.util
import sys
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
ADDON = ROOT / "odoo_addons" / "sf_connector"
MODEL = ADDON / "models" / "account_move.py"
TEMPLATE = ADDON / "report" / "report_invoice.xml"
VIEW = ADDON / "views" / "account_move_views.xml"
MIGRATION = ADDON / "migrations" / "19.0.1.8.12" / "post-migrate.py"


class _Anything:
    """Stands in for odoo.fields: every field definition is accepted and ignored."""
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _odoo_standin():
    odoo = types.ModuleType("odoo")
    odoo.fields = _Anything()
    odoo.models = types.SimpleNamespace(Model=object, BaseModel=object, TransientModel=object)
    odoo.SUPERUSER_ID = 1
    odoo.api = types.SimpleNamespace(Environment=None)
    exceptions = types.ModuleType("odoo.exceptions")
    exceptions.UserError = type("UserError", (Exception,), {})
    odoo.exceptions = exceptions
    return {"odoo": odoo, "odoo.exceptions": exceptions}


def load_model():
    with mock.patch.dict(sys.modules, _odoo_standin()):
        spec = importlib.util.spec_from_file_location("account_move_under_test", MODEL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module.AccountMove


class Ensure:
    def ensure_one(self):
        return None


class Sale(types.SimpleNamespace):
    pass


class Recs(list):
    """Stands in for an Odoo recordset: a slice is a recordset, and a field read on it reads the first record."""
    def __getitem__(self, index):
        if isinstance(index, slice):
            return Recs(list.__getitem__(self, index))
        return list.__getitem__(self, index)

    def __getattr__(self, name):
        return getattr(self[0], name) if self else False


class Line:
    def __init__(self, sf=False, display="product", sale_lines=()):
        self.sf_combined_description = sf
        self.display_type = display
        self.sale_line_ids = Recs(sale_lines)


class Move(Ensure):
    def __init__(self, move_type="out_invoice", state="posted", lines=(), company=None):
        self.move_type = move_type
        self.state = state
        self.invoice_line_ids = list(lines)
        self.company_id = company


class TitleAndBoxTests(unittest.TestCase):
    def setUp(self):
        self.AccountMove = load_model()

    def test_title_for_posted_invoice_draft_and_credit_note(self):
        title = self.AccountMove.sf_document_title
        self.assertEqual(title(Move("out_invoice", "posted")), "Invoice")
        self.assertEqual(title(Move("out_invoice", "draft")), "Draft Invoice")
        self.assertEqual(title(Move("out_refund", "posted")), "Credit Note")
        self.assertEqual(title(Move("out_refund", "draft")), "Draft Credit Note")

    def test_credit_notes_have_no_early_payment_offers(self):
        company = types.SimpleNamespace(sf_epd_days_1=10, sf_epd_percent_1=3, sf_epd_days_2=0, sf_epd_percent_2=0)
        move = Move("out_refund", "posted", company=company)
        move.invoice_date = None
        move.amount_total = 100.0
        self.assertEqual(self.AccountMove.sf_early_payment_offers(move), [])


class DescriptionCopyTests(unittest.TestCase):
    def setUp(self):
        self.AccountMove = load_model()

    def fill(self, moves):
        self.AccountMove._sf_fill_description_copies(moves)

    def test_empty_copy_is_filled_from_the_sale_line(self):
        sale = Sale(sf_combined_description="2026 Q1 USA")
        line = Line(sf=False, sale_lines=[sale])
        move = Move(lines=[line])
        self.fill([move])
        self.assertEqual(line.sf_combined_description, "2026 Q1 USA")

    def test_existing_copy_is_kept(self):
        line = Line(sf="kept", sale_lines=[Sale(sf_combined_description="new")])
        self.fill([Move(lines=[line])])
        self.assertEqual(line.sf_combined_description, "kept")

    def test_sale_line_without_text_leaves_the_copy_empty(self):
        line = Line(sf=False, sale_lines=[Sale(sf_combined_description=False)])
        self.fill([Move(lines=[line])])
        self.assertFalse(line.sf_combined_description)

    def test_section_and_manual_lines_are_skipped(self):
        section = Line(sf=False, display="line_section", sale_lines=[Sale(sf_combined_description="x")])
        manual = Line(sf=False, sale_lines=())
        self.fill([Move(lines=[section, manual])])
        self.assertFalse(section.sf_combined_description)
        self.assertFalse(manual.sf_combined_description)


class MigrationTests(unittest.TestCase):
    """The 19.0.1.8.12 backfill on existing invoice lines."""

    def load(self, lines):
        class Model:
            def search(self, domain):
                self.domain = domain  # the database applies this: only lines with an empty copy come back
                return [line for line in lines if not line.sf_combined_description]

        model = Model()
        env = {"account.move.line": model}
        odoo = _odoo_standin()["odoo"]
        odoo.api = types.SimpleNamespace(Environment=lambda cr, uid, ctx: env)
        with mock.patch.dict(sys.modules, {"odoo": odoo, "odoo.exceptions": odoo.exceptions}):
            spec = importlib.util.spec_from_file_location("post_migrate_19_0_1_8_12", MIGRATION)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            changed = module.migrate(None, "19.0.1.8.12")
        return changed, model.domain

    def test_fills_only_empty_copies_with_a_sale_line_description(self):
        filled = Line(sf=False, sale_lines=[Sale(sf_combined_description="2026 Q1 USA")])
        kept = Line(sf="already", sale_lines=[Sale(sf_combined_description="other")])
        nothing = Line(sf=False, sale_lines=[Sale(sf_combined_description=False)])
        changed, domain = self.load([filled, kept, nothing])
        self.assertEqual(changed, 1)
        self.assertEqual(filled.sf_combined_description, "2026 Q1 USA")
        self.assertEqual(kept.sf_combined_description, "already")
        self.assertFalse(nothing.sf_combined_description)
        self.assertIn(("sf_combined_description", "=", False), domain)


class TemplateAndViewTests(unittest.TestCase):
    def test_title_comes_from_the_helper_not_a_fixed_word(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("o.sf_document_title()", text)
        self.assertNotIn('>Invoice <strong t-esc="o.name"/>', text)

    def test_model_defines_the_helpers_the_template_and_tests_use(self):
        text = MODEL.read_text(encoding="utf-8")
        for name in ("sf_document_title", "action_post", "_sf_fill_description_copies"):
            self.assertIn(f"def {name}(", text)

    def test_item_description_column_is_read_only_on_the_invoice_form(self):
        root = ET.parse(VIEW).getroot()
        field = root.find(".//field[@name='sf_combined_description']")
        self.assertIsNotNone(field)
        self.assertEqual(field.get("readonly"), "1")
        self.assertEqual(field.get("string"), "Item Description")
        xpath = root.find(".//xpath")
        self.assertIn("invoice_line_ids", xpath.get("expr"))

    def test_view_is_in_the_manifest(self):
        self.assertIn("views/account_move_views.xml", (ADDON / "__manifest__.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
