"""Salesforce access.

get_session() is the only place that handles authentication. It borrows a
login from the sf CLI. To switch to the OAuth client-credentials flow, replace
the body of get_session() so it returns a SalesforceSession. Nothing else needs
to change.
"""
import json
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from . import config


class SalesforceError(Exception):
    pass


class SalesforceSession:
    def __init__(self, instance_url, access_token):
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token


def _sf_cli_json(args):
    exe = shutil.which("sf")
    if not exe:
        raise SalesforceError("sf CLI not found on PATH")
    proc = subprocess.run(
        [exe, *args, "--json"], capture_output=True, text=True, encoding="utf-8", timeout=120
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise SalesforceError(f"sf {' '.join(args)} failed: {detail}") from None
    if proc.returncode != 0 or payload.get("status", 0) != 0:
        raise SalesforceError(f"sf {' '.join(args)} failed: {payload.get('message', 'unknown error')}")
    return payload["result"]


def get_session(target_org=None):
    org = target_org or config.SF_TARGET_ORG
    display = _sf_cli_json(["org", "display", "--target-org", org])
    # `org display` shows a redacted accessToken in sf CLI 2.x, so the token comes from show-access-token.
    token = _sf_cli_json(["org", "auth", "show-access-token", "--target-org", org])["accessToken"]
    return SalesforceSession(display["instanceUrl"], token)


class SalesforceClient:
    def __init__(self, session):
        self.session = session
        self.api_version = self._latest_api_version()

    def _request(self, path_or_url):
        url = path_or_url if path_or_url.startswith("http") else self.session.instance_url + path_or_url
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.session.access_token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")[:500]
            raise SalesforceError(f"HTTP {err.code} from Salesforce: {body}") from None

    def _latest_api_version(self):
        versions = self._request("/services/data/")
        return max((v["version"] for v in versions), key=float)

    def query(self, soql):
        """Run a SOQL query and return all records, following pagination."""
        data = self._request(
            f"/services/data/v{self.api_version}/query/?q={urllib.parse.quote(soql, safe='')}"
        )
        records = list(data["records"])
        while not data["done"]:
            data = self._request(data["nextRecordsUrl"])
            records.extend(data["records"])
        for record in records:
            record.pop("attributes", None)
        return records
