"""OdooClient sends the configured companies with every call, so no query depends on the API user's default."""
import unittest

from connector.odoo_client import OdooClient


class CompanyContextTests(unittest.TestCase):
    def client(self, company_ids):
        return OdooClient("http://odoo.test", "db", "key", company_ids=company_ids)

    def test_no_companies_configured_sends_no_context(self):
        self.assertEqual(self.client(None)._with_companies({"domain": []}), {"domain": []})

    def test_configured_companies_go_in_the_context_of_every_call(self):
        out = self.client([1, 2])._with_companies({"domain": []})
        self.assertEqual(out["context"], {"allowed_company_ids": [1, 2]})

    def test_existing_context_is_kept(self):
        out = self.client([2])._with_companies({"context": {"active_test": False}})
        self.assertEqual(out["context"], {"active_test": False, "allowed_company_ids": [2]})


if __name__ == "__main__":
    unittest.main()
