"""Minimal client for the Odoo 19 JSON-2 API.

Each call is POST {url}/json/2/<model>/<method> with the method's keyword
arguments as a JSON body. The API key goes in a Bearer Authorization header.
"""
import json
import urllib.error
import urllib.request


class OdooError(Exception):
    pass


def m2o_id(value):
    """Return the id from a many2one value (an id, an [id, name] pair, or False)."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else False
    return value or False


class OdooClient:
    def __init__(self, url, db, api_key, timeout=300):
        if not (url and db and api_key):
            raise OdooError("ODOO_URL, ODOO_DB and ODOO_API_KEY must be set (connector/.env)")
        self.base_url = url.rstrip("/")
        self.db = db
        self.api_key = api_key
        self.timeout = timeout

    def call(self, model, method, **kwargs):
        request = urllib.request.Request(
            f"{self.base_url}/json/2/{model}/{method}",
            data=json.dumps(kwargs).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "X-Odoo-Database": self.db,
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            raise self._http_error(err) from None
        except urllib.error.URLError as err:
            raise OdooError(f"cannot reach {self.base_url}: {err.reason}") from None
        return json.loads(raw) if raw else None

    def search_read(self, model, domain, fields, **kwargs):
        return self.call(model, "search_read", domain=domain, fields=fields, **kwargs)

    def create(self, model, vals_list):
        return self.call(model, "create", vals_list=vals_list)

    def write(self, model, ids, vals):
        return self.call(model, "write", ids=ids, vals=vals)

    @staticmethod
    def _http_error(err):
        body = err.read().decode("utf-8", "replace")
        try:
            message = json.loads(body).get("message") or body
        except (ValueError, AttributeError):
            message = body
        return OdooError(f"HTTP {err.code} from Odoo: {str(message)[:500]}")
