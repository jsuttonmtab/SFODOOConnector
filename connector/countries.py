"""Country and state matching for Salesforce billing/shipping text (docs/field-mapping.md §4.1a).

Country: case-insensitive match on the Odoo name, then on the ISO code, then the alias table.
No match returns None, and the caller leaves the field blank and logs it.

State: within the country, try the name, then the code. Matching ignores case, accents,
dots and hyphens, and drops prefixes such as "Prov." and suffixes such as "-ken" and
"Prefecture". Both sides are normalised the same way.
"""
import re
import unicodedata

from . import config
from .odoo_client import m2o_id

_STATE_PREFIX = re.compile(r"^(prov|province)\s+")
_STATE_SUFFIX = re.compile(r"\s+(ken|prefecture|prov|province)$")


def normalize_state(text):
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace(".", " ").replace("-", " ")
    s = " ".join(s.split())
    s = _STATE_PREFIX.sub("", s)
    return _STATE_SUFFIX.sub("", s)


class AddressMatcher:
    def __init__(self, countries, states):
        """countries: rows with id, name, code. states: rows with id, name, code, country_id."""
        self.country_by_name = {}
        self.country_by_code = {}
        for country in countries:
            self.country_by_name.setdefault(country["name"].lower(), country["id"])
            if country["code"]:
                self.country_by_code.setdefault(country["code"].lower(), country["id"])

        self.state_by_name = {}
        self.state_by_code = {}
        for state in states:
            country_id = m2o_id(state["country_id"])
            if state["name"]:
                self.state_by_name.setdefault((country_id, normalize_state(state["name"])), state["id"])
            if state["code"]:
                self.state_by_code.setdefault((country_id, normalize_state(state["code"])), state["id"])

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
        key = normalize_state(text)
        if not country_id or not key:
            return None
        return self.state_by_name.get((country_id, key)) or self.state_by_code.get((country_id, key))
