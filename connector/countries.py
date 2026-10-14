"""Country and state matching for Salesforce billing/shipping text (docs/field-mapping.md §4.1a).

Country: case-insensitive match on the Odoo name, then on the ISO code, then the alias table.
No match returns None, and the caller leaves the field blank and logs it.
"""
from . import config
from .odoo_client import m2o_id


class AddressMatcher:
    def __init__(self, countries, states):
        """countries: rows with id, name, code. states: rows with id, name, code, country_id."""
        self.country_by_name = {}
        self.country_by_code = {}
        for country in countries:
            self.country_by_name.setdefault(country["name"].lower(), country["id"])
            if country["code"]:
                self.country_by_code.setdefault(country["code"].lower(), country["id"])

        self.state_by_country = {}
        for state in states:
            country_id = m2o_id(state["country_id"])
            for key in (state["name"], state["code"]):
                if key:
                    self.state_by_country.setdefault((country_id, key.lower()), state["id"])

    def country_id(self, text):
        key = (text or "").strip().lower()
        if not key:
            return None
        if key in self.country_by_name:
            return self.country_by_name[key]
        if key in self.country_by_code:
            return self.country_by_code[key]
        code = config.COUNTRY_ALIASES.get(key)
        return self.country_by_code.get(code.lower()) if code else None

    def state_id(self, country_id, text):
        key = (text or "").strip().lower()
        if not country_id or not key:
            return None
        return self.state_by_country.get((country_id, key))
