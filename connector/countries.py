"""Country and state matching for Salesforce billing/shipping text (docs/field-mapping.md §4.1a).

Country: case-insensitive match on the Odoo name, then on the ISO code, then the alias table.
No match returns None, and the caller leaves the field blank and logs it.

State: within the country, try the name, then the code. Matching ignores case, accents,
dots and hyphens, and drops prefixes such as "Prov." and suffixes such as "-ken" and
"Prefecture". Both sides are normalised the same way. For Japan, English prefecture names
(with -ken, -to, -fu, -do or "Prefecture") are also matched to Odoo's JP-xx codes.
"""
import re
import unicodedata

from . import config
from .odoo_client import m2o_id

_STATE_PREFIX = re.compile(r"^(prov|province)\s+")
_STATE_SUFFIX = re.compile(r"\s+(ken|prefecture|prov|province)$")
_JP_SUFFIX = re.compile(r"\s+(to|fu|do)$")

# English names of Japan's 47 prefectures (base name, no -ken/-to/-fu/-do) -> JP prefecture number.
# Odoo stores the prefectures in Japanese under codes JP-01 ... JP-47, so these are matched by code.
JP_PREFECTURES = {
    "hokkaido": 1,
    "aomori": 2,
    "iwate": 3,
    "miyagi": 4,
    "akita": 5,
    "yamagata": 6,
    "fukushima": 7,
    "ibaraki": 8,
    "tochigi": 9,
    "gunma": 10,
    "saitama": 11,
    "chiba": 12,
    "tokyo": 13,
    "kanagawa": 14,
    "niigata": 15,
    "toyama": 16,
    "ishikawa": 17,
    "fukui": 18,
    "yamanashi": 19,
    "nagano": 20,
    "gifu": 21,
    "shizuoka": 22,
    "aichi": 23,
    "mie": 24,
    "shiga": 25,
    "kyoto": 26,
    "osaka": 27,
    "hyogo": 28,
    "nara": 29,
    "wakayama": 30,
    "tottori": 31,
    "shimane": 32,
    "okayama": 33,
    "hiroshima": 34,
    "yamaguchi": 35,
    "tokushima": 36,
    "kagawa": 37,
    "ehime": 38,
    "kochi": 39,
    "fukuoka": 40,
    "saga": 41,
    "nagasaki": 42,
    "kumamoto": 43,
    "oita": 44,
    "miyazaki": 45,
    "kagoshima": 46,
    "okinawa": 47,
}


def japan_prefecture_code(key):
    """'Tokyo', 'Tokyo-to', 'Kyoto Fu', 'Kanagawa Prefecture' -> 'JP-13' (already normalised input)."""
    number = JP_PREFECTURES.get(_JP_SUFFIX.sub("", key))
    return f"JP-{number:02d}" if number else None


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
        self.code_by_id = {}
        for country in countries:
            self.country_by_name.setdefault(country["name"].lower(), country["id"])
            if country["code"]:
                self.country_by_code.setdefault(country["code"].lower(), country["id"])
                self.code_by_id[country["id"]] = country["code"].upper()

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
        found = self.state_by_name.get((country_id, key)) or self.state_by_code.get((country_id, key))
        if found or self.code_by_id.get(country_id) != "JP":
            return found
        code = japan_prefecture_code(key)
        return self.state_by_code.get((country_id, normalize_state(code))) if code else None
