"""GitHub Actions OIDC client for the candidate cloud-ingest gateway."""

from __future__ import annotations

import json
import os
import time
import base64
import urllib.error
import urllib.parse
import urllib.request
import uuid


class OIDCGatewayClient:
    def __init__(self, endpoint: str, audience: str, *, retries: int = 3):
        self.endpoint = endpoint
        self.audience = audience
        self.retries = retries
        self.request_count = 0
        self._token = None
        self._token_exp = 0

    @staticmethod
    def _token_expiry(token: str) -> int:
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return int(json.loads(base64.urlsafe_b64decode(payload).decode("utf-8")).get("exp", 0))
        except (IndexError, ValueError, TypeError, json.JSONDecodeError):
            return 0

    def _oidc_token(self):
        if self._token and self._token_exp > int(time.time()) + 30:
            return self._token
        url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
        bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
        if not url or not bearer:
            raise RuntimeError("GitHub OIDC environment is unavailable")
        separator = "&" if "?" in url else "?"
        request = urllib.request.Request(url + separator + urllib.parse.urlencode({"audience": self.audience}), headers={"Authorization": f"Bearer {bearer}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.loads(response.read().decode("utf-8")).get("value")
        if not value:
            raise RuntimeError("GitHub OIDC token was not returned")
        self._token = value
        self._token_exp = self._token_expiry(value)
        if self._token_exp <= int(time.time()):
            self._token = None
            raise RuntimeError("GitHub OIDC token is expired or malformed")
        return value

    def call(self, payload):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > 1_200_000:
            raise ValueError("gateway request exceeds size limit")
        last = None
        for attempt in range(self.retries):
            self.request_count += 1
            try:
                headers = {"Authorization": f"Bearer {self._oidc_token()}", "Content-Type": "application/json", "User-Agent": "KeirinCloudOIDC/1.0"}
                with urllib.request.urlopen(urllib.request.Request(self.endpoint, data=data, headers=headers, method="POST"), timeout=60) as response:
                    raw = response.read()
                    return json.loads(raw.decode("utf-8")) if raw else None
            except urllib.error.HTTPError as exc:
                last = RuntimeError(f"cloud gateway HTTP {exc.code}")
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise last from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                last = RuntimeError("cloud gateway unavailable")
            if attempt + 1 < self.retries:
                time.sleep(2 ** attempt)
                self._token = None
                self._token_exp = 0
        raise last or RuntimeError("cloud gateway failed")


class OIDCGatewayStore:
    def __init__(self, client: OIDCGatewayClient):
        self.client = client

    def get(self, record_type, record_key):
        result = self.client.call({"operation": "read", "record_type": record_type, "record_key": record_key})
        return (result or {}).get("record")

    def cas_put(self, record, expected_hash):
        stable = f'{record["record_type"]}\0{record["record_key"]}\0{record["content_hash"]}\0{record.get("source_updated_at") or ""}\0{expected_hash or ""}'
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, stable))
        result = self.client.call({
            "operation": "write", "request_id": request_id,
            "record_type": record["record_type"], "record_key": record["record_key"],
            "payload_json": json.dumps(record["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "content_hash": record["content_hash"],
            "source_updated_at": record.get("source_updated_at"), "expected_hash": expected_hash,
        })
        row = result[0] if isinstance(result, list) and result else result or {}
        return row.get("action") in {"insert", "update", "unchanged"} and row.get("stored_hash") == record["content_hash"]


