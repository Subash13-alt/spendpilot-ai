"""PayPal Sandbox Payouts client. Credentials come from the environment only and never reach templates/JS.

Nothing here fabricates a result: every status is read from PayPal's server-side API responses.
"""
import base64
import time
from typing import Optional

import requests

TIMEOUT = 20

# PayPal payout *item* transaction_status values
ITEM_SUCCESS = {"SUCCESS"}
ITEM_FAILED = {"FAILED", "DENIED", "RETURNED", "REFUNDED", "REVERSED", "BLOCKED", "CANCELED"}
ITEM_PENDING = {"PENDING", "ONHOLD", "UNCLAIMED", "PROCESSING"}


class PayPalError(Exception):
    def __init__(self, message, status_code=None, outcome_unknown=False):
        super().__init__(message)
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown  # True when the request may have reached PayPal


def classify_item_status(status: Optional[str]) -> str:
    """Map a PayPal item status to PAID / FAILED / PENDING."""
    s = (status or "").upper()
    if s in ITEM_SUCCESS:
        return "PAID"
    if s in ITEM_FAILED:
        return "FAILED"
    return "PENDING"


class PayPalClient:
    def __init__(self, client_id: str, client_secret: str, base_url: str, session=None):
        self.client_id, self.client_secret = client_id, client_secret
        self.base_url = base_url.rstrip("/")
        if "sandbox" not in self.base_url:
            raise ValueError("SpendPilot AI is restricted to the PayPal Sandbox endpoint")
        self.http = session or requests.Session()
        self._token, self._expires = None, 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _auth(self) -> str:
        if self._token and time.time() < self._expires - 30:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        try:
            r = self.http.post(f"{self.base_url}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                               headers={"Authorization": f"Basic {basic}"}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise PayPalError(f"Could not reach PayPal: {exc.__class__.__name__}")  # safe: nothing submitted yet
        if r.status_code != 200:
            raise PayPalError(f"PayPal authentication failed (HTTP {r.status_code})", r.status_code)
        body = r.json()
        self._token = body["access_token"]
        self._expires = time.time() + int(body.get("expires_in", 300))
        return self._token

    def create_payout(self, sender_batch_id: str, sender_item_id: str, receiver_email: str,
                      amount: float, currency: str, note: str) -> dict:
        token = self._auth()
        payload = {
            "sender_batch_header": {"sender_batch_id": sender_batch_id, "email_subject": "SpendPilot AI reimbursement",
                                    "email_message": "Your approved expense has been reimbursed."},
            "items": [{"recipient_type": "EMAIL", "receiver": receiver_email, "sender_item_id": sender_item_id,
                       "note": note[:150], "amount": {"value": f"{amount:.2f}", "currency": currency}}],
        }
        try:
            r = self.http.post(f"{self.base_url}/v1/payments/payouts", json=payload, timeout=TIMEOUT,
                               headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        except requests.RequestException as exc:
            raise PayPalError(f"Payout request did not complete ({exc.__class__.__name__}); outcome unknown",
                              outcome_unknown=True)
        if r.status_code not in (200, 201):
            msg = _error_message(r)
            raise PayPalError(f"PayPal rejected the payout (HTTP {r.status_code}): {msg}", r.status_code,
                              outcome_unknown=r.status_code >= 500)
        return r.json()

    def get_payout(self, payout_batch_id: str) -> dict:
        token = self._auth()
        try:
            r = self.http.get(f"{self.base_url}/v1/payments/payouts/{payout_batch_id}",
                              headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise PayPalError(f"Could not verify payout ({exc.__class__.__name__})", outcome_unknown=True)
        if r.status_code != 200:
            raise PayPalError(f"Payout lookup failed (HTTP {r.status_code}): {_error_message(r)}", r.status_code)
        return r.json()


def _error_message(r) -> str:
    try:
        b = r.json()
        return b.get("message") or b.get("error_description") or b.get("name") or "unknown error"
    except ValueError:
        return "non-JSON error response"


def client_from_config(config) -> Optional[PayPalClient]:
    """Return a client only when both credentials are configured, else None (=> labelled demo mode)."""
    if not (config.get("PAYPAL_CLIENT_ID") and config.get("PAYPAL_CLIENT_SECRET")):
        return None
    return PayPalClient(config["PAYPAL_CLIENT_ID"], config["PAYPAL_CLIENT_SECRET"], config["PAYPAL_BASE_URL"])
