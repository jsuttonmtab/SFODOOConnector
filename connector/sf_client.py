"""Salesforce access.

get_session() is the only place that handles authentication. It returns one SalesforceSession per
process (one sync run), built on first use:

- OAuth client-credentials flow (POST {SF_LOGIN_URL}/services/oauth2/token) when SF_CLIENT_ID and
  SF_CLIENT_SECRET are set. This is the integration user's External Client App.
- Otherwise the sf CLI login for SF_TARGET_ORG (the interim setup, see docs/operations.md).

A 401 from Salesforce refreshes the session once and retries the request. Credentials are never printed.
"""
import json
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from . import config


class SalesforceError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class SalesforceSession:
    def __init__(self, instance_url, access_token, org_id=None, refresh=None):
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.org_id = org_id  # 18-character Id of the authenticated org, None if unknown
        self._refresh = refresh  # callable returning a new SalesforceSession

    def refresh(self):
        if self._refresh is None:
            raise SalesforceError("Salesforce returned 401 and this session cannot be refreshed")
        fresh = self._refresh()
        self.instance_url = fresh.instance_url
        self.access_token = fresh.access_token
        self.org_id = fresh.org_id


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


def _cli_session(target_org):
    org = target_org or config.SF_TARGET_ORG
    display = _sf_cli_json(["org", "display", "--target-org", org])
    # `org display` shows a redacted accessToken in sf CLI 2.x, so the token comes from show-access-token.
    token = _sf_cli_json(["org", "auth", "show-access-token", "--target-org", org])["accessToken"]
    return SalesforceSession(display["instanceUrl"], token, display.get("id"), refresh=lambda: _cli_session(org))


def _org_id_from_identity(url):
    """Identity URL from the token response: https://login.salesforce.com/id/<orgId>/<userId>."""
    parts = str(url).split("/id/")
    return parts[1].split("/")[0] if len(parts) == 2 else None


def _oauth_session():
    url = config.SF_LOGIN_URL + "/services/oauth2/token"
    form = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": config.SF_CLIENT_ID,
        "client_secret": config.SF_CLIENT_SECRET,
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=form, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")[:300]
        raise SalesforceError(f"OAuth token request failed: HTTP {err.code} {body}") from None
    except urllib.error.URLError as err:
        raise SalesforceError(f"cannot reach {config.SF_LOGIN_URL}: {err.reason}") from None
    return SalesforceSession(
        payload["instance_url"], payload["access_token"], _org_id_from_identity(payload.get("id")),
        refresh=_oauth_session,
    )


_session = None


def get_session(target_org=None):
    """The session for this process. Built on first use, then reused for every call."""
    global _session
    if _session is None:
        if bool(config.SF_CLIENT_ID) != bool(config.SF_CLIENT_SECRET):
            raise SalesforceError("set both SF_CLIENT_ID and SF_CLIENT_SECRET, or neither (connector/.env)")
        if config.SF_CLIENT_ID:
            if not config.SF_LOGIN_URL:
                raise SalesforceError("SF_LOGIN_URL must be set for the client-credentials login")
            _session = _oauth_session()
        else:
            _session = _cli_session(target_org)
    return _session


class SalesforceClient:
    def __init__(self, session):
        self.session = session
        self.api_version = self._latest_api_version()

    def _send(self, method, url, body):
        headers = {"Authorization": f"Bearer {self.session.access_token}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", "replace")[:500]
            raise SalesforceError(f"HTTP {err.code} from Salesforce: {text}", status=err.code) from None
        return json.loads(raw) if raw else None

    def request(self, method, path_or_url, body=None):
        """Call Salesforce and return the decoded JSON. A 401 refreshes the session once, then retries."""
        try:
            return self._send(method, self._url(path_or_url), body)
        except SalesforceError as err:
            if err.status != 401:
                raise
        self.session.refresh()
        return self._send(method, self._url(path_or_url), body)

    def _url(self, path_or_url):
        return path_or_url if path_or_url.startswith("http") else self.session.instance_url + path_or_url

    def _latest_api_version(self):
        versions = self.request("GET", "/services/data/")
        return max((v["version"] for v in versions), key=float)

    def describe(self, sobject):
        return self.request("GET", f"/services/data/v{self.api_version}/sobjects/{sobject}/describe")

    def query(self, soql):
        """Run a SOQL query and return all records, following pagination."""
        data = self.request("GET", f"/services/data/v{self.api_version}/query/?q={urllib.parse.quote(soql, safe='')}")
        records = list(data["records"])
        while not data["done"]:
            data = self.request("GET", data["nextRecordsUrl"])
            records.extend(data["records"])
        for record in records:
            record.pop("attributes", None)
        return records
