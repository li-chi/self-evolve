"""`google.oauth2.service_account` shim.

Toolathlon code loads a service-account JSON purely to hand credentials to
a client; the mock ignores them, so this just parses the file and keeps the
project id so `Credentials.project_id` behaves.
"""

import json


class Credentials:
    def __init__(self, info=None, scopes=None, **kwargs):
        self._info = info or {}
        self.scopes = scopes
        self.project_id = self._info.get("project_id")
        self.service_account_email = self._info.get(
            "client_email", "mock@mock-project.iam.gserviceaccount.com")
        self.token = "mock-token"
        self.valid = True
        self.expired = False

    @classmethod
    def from_service_account_file(cls, filename, scopes=None, **kwargs):
        with open(filename, "r", encoding="utf-8") as f:
            info = json.load(f)
        return cls(info=info, scopes=scopes, **kwargs)

    @classmethod
    def from_service_account_info(cls, info, scopes=None, **kwargs):
        return cls(info=info, scopes=scopes, **kwargs)

    def with_scopes(self, scopes):
        c = Credentials(info=self._info, scopes=scopes)
        return c

    def refresh(self, request=None):
        self.valid = True
