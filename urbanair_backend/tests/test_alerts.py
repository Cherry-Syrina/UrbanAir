"""
Tests for the alert / email-notification endpoints (/api/alerts/*).

The email provider (Resend HTTPS API, and the optional SMTP fallback) is
MOCKED here -- these tests exercise the wiring, recipient validation, provider
acceptance/rejection and timeout/network error handling; they do NOT deliver
real email. A genuine delivery test must be run manually against a controlled
inbox with a real provider key configured.
"""

import os
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import requests

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


CURRENT = {"aqi": 175.0, "category": "Moderate", "city": "Delhi"}
FORECASTS = [
    {"horizon_hours": 24, "predicted_aqi": 133.0, "predicted_category": "Moderate"},
]
ATTRIBUTION = {
    "point": "Delhi",
    "sources": {
        "traffic_pct": 36.4, "industrial_pct": 16.0, "construction_pct": 0.0,
        "waste_burning_pct": 0.8, "background_pct": 46.9,
    },
    "raw_signals": {},
}
ENFORCEMENT = {
    "point": "Delhi",
    "reasons": ["AQI is at 175 (Moderate)"],
    "recommended_actions": ["Deploy traffic police"],
}


def _context(point):
    return CURRENT, list(FORECASTS), dict(ATTRIBUTION), dict(ENFORCEMENT)


class AlertEndpointsTestCase(TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from app import main
        return TestClient(main.app), main

    def _send(self, client, **overrides):
        payload = {"point": "Delhi", "alert_type": "official", "to_email": "someone@example.com"}
        payload.update(overrides)
        return client.post("/api/alerts/send", json=payload)


class TestAlertStatus(AlertEndpointsTestCase):
    def test_reports_smtp_configured(self):
        client, main = self._client()
        with patch.object(main.notifications, "is_configured", return_value=True):
            resp = client.get("/api/alerts/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"smtp_configured": True})

    def test_reports_smtp_missing(self):
        client, main = self._client()
        with patch.object(main.notifications, "is_configured", return_value=False):
            resp = client.get("/api/alerts/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"smtp_configured": False})


class TestAlertPreviews(AlertEndpointsTestCase):
    def test_official_preview_returns_report_text(self):
        client, main = self._client()
        with patch.object(main, "_get_alert_context", side_effect=_context):
            resp = client.get("/api/alerts/officials/Delhi")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["point"], "Delhi")
        self.assertIn("URBANAIR AI", body["report_text"])
        self.assertIn("RECOMMENDED ACTIONS", body["report_text"])

    def test_public_preview_returns_advisory(self):
        client, main = self._client()
        with patch.object(main, "_get_alert_context", side_effect=_context):
            resp = client.get("/api/alerts/public/Delhi")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["point"], "Delhi")
        self.assertIn("UrbanAir AI Alert", body["message"])


class TestAlertSend(AlertEndpointsTestCase):
    def test_rejects_invalid_recipient(self):
        client, _ = self._client()
        resp = self._send(client, to_email="not-an-email")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("valid recipient email", resp.json()["detail"])

    def test_rejects_unknown_alert_type(self):
        client, main = self._client()
        with patch.object(main, "_get_alert_context", side_effect=_context):
            resp = self._send(client, alert_type="sms")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("alert_type", resp.json()["detail"])

    def test_smtp_not_configured_is_503_with_message(self):
        client, main = self._client()
        with patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications, "send_email",
                             side_effect=RuntimeError("SMTP is not configured")):
            resp = self._send(client)
        self.assertEqual(resp.status_code, 503)
        self.assertIn("SMTP", resp.json()["detail"])

    def test_timeout_is_mapped_to_503(self):
        client, main = self._client()
        with patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications, "send_email",
                             side_effect=TimeoutError("timeout")):
            resp = self._send(client)
        self.assertEqual(resp.status_code, 503)
        self.assertIn("timed out", resp.json()["detail"])

    def test_smtp_generic_failure_returns_500(self):
        client, main = self._client()
        with patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications, "send_email",
                             side_effect=Exception("some smtp error")):
            resp = self._send(client)
        self.assertEqual(resp.status_code, 500)
        self.assertIn("Failed to send", resp.json()["detail"])

    def test_successful_send_reports_recipient_and_subject(self):
        client, main = self._client()
        with patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications, "send_email",
                             return_value={"to": "someone@example.com"}) as mocked:
            resp = self._send(client)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["to"], "someone@example.com")
        self.assertIn("subject", body)
        self.assertEqual(mocked.call_count, 1)

    def test_public_advisory_send_uses_message_body(self):
        client, main = self._client()
        with patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications, "send_email",
                             return_value={"to": "pub@example.com"}) as mocked:
            resp = self._send(client, alert_type="public")
        self.assertEqual(resp.status_code, 200)
        to_email, subject, body = mocked.call_args.args
        self.assertEqual(to_email, "someone@example.com")
        self.assertIn("Air Quality Advisory", subject)
        self.assertIn("UrbanAir AI Alert", body)


class _FakeResponse:
    """Minimal stand-in for a requests.Response from the email provider."""

    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


RESEND_ENV = {"RESEND_API_KEY": "secret_test_key", "RESEND_FROM_EMAIL": "UrbanAir <alerts@urbanair.test>"}


class TestProviderSelection(TestCase):
    def test_resend_is_preferred_over_smtp(self):
        from app import notifications
        with patch.dict(os.environ, {**RESEND_ENV, "SMTP_EMAIL": "me@example.com", "SMTP_APP_PASSWORD": "pw"}):
            self.assertEqual(notifications.provider(), "resend")
            self.assertTrue(notifications.is_configured())

    def test_provider_is_none_when_nothing_configured(self):
        from app import notifications
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(notifications.provider())
            self.assertFalse(notifications.is_configured())


class TestResendSend(AlertEndpointsTestCase):
    """Resend HTTPS provider, mocked at the requests.post boundary."""

    def test_resend_acceptance_reports_provider_and_id(self):
        client, main = self._client()
        fake = _FakeResponse(200, {"id": "resend-id-123"})
        with patch.dict(os.environ, RESEND_ENV), \
                patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications.requests, "post", return_value=fake) as post:
            resp = self._send(client)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["to"], "someone@example.com")
        self.assertEqual(body["provider"], "resend")
        self.assertEqual(body["id"], "resend-id-123")
        self.assertIn("subject", body)

        args, kwargs = post.call_args
        self.assertEqual(args[0], main.notifications.RESEND_API_URL)
        connect, read = kwargs["timeout"]
        self.assertGreater(connect, 0)
        self.assertGreater(read, 0)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret_test_key")
        self.assertEqual(kwargs["json"]["to"], ["someone@example.com"])

    def test_resend_invalid_recipient_is_400_and_never_calls_provider(self):
        client, main = self._client()
        with patch.dict(os.environ, RESEND_ENV), \
                patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications.requests, "post") as post:
            resp = self._send(client, to_email="not-an-email")
        self.assertEqual(resp.status_code, 400)
        post.assert_not_called()

    def test_resend_provider_rejection_is_502_without_leaking_key(self):
        client, main = self._client()
        fake = _FakeResponse(422, {"statusCode": 422, "message": "Invalid `to` field"})
        with patch.dict(os.environ, RESEND_ENV), \
                patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications.requests, "post", return_value=fake):
            resp = self._send(client)
        self.assertEqual(resp.status_code, 502)
        detail = resp.json()["detail"]
        self.assertIn("rejected", detail.lower())
        self.assertIn("422", detail)
        self.assertIn("Invalid `to` field", detail)
        self.assertNotIn("secret_test_key", detail)

    def test_resend_auth_error_is_502_without_leaking_key(self):
        client, main = self._client()
        fake = _FakeResponse(401, {"message": "API key is invalid"})
        with patch.dict(os.environ, RESEND_ENV), \
                patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications.requests, "post", return_value=fake):
            resp = self._send(client)
        self.assertEqual(resp.status_code, 502)
        self.assertNotIn("secret_test_key", resp.json()["detail"])

    def test_resend_timeout_is_503(self):
        client, main = self._client()
        with patch.dict(os.environ, RESEND_ENV), \
                patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications.requests, "post",
                             side_effect=requests.exceptions.Timeout("too slow")):
            resp = self._send(client)
        self.assertEqual(resp.status_code, 503)
        self.assertIn("timed out", resp.json()["detail"].lower())

    def test_resend_network_error_is_503(self):
        client, main = self._client()
        with patch.dict(os.environ, RESEND_ENV), \
                patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications.requests, "post",
                             side_effect=requests.exceptions.ConnectionError("network unreachable")):
            resp = self._send(client)
        self.assertEqual(resp.status_code, 503)
        detail = resp.json()["detail"]
        self.assertIn("reach", detail.lower())
        self.assertNotIn("secret_test_key", detail)


class TestEmailNotConfigured(AlertEndpointsTestCase):
    def test_missing_provider_is_503_and_never_calls_provider(self):
        client, main = self._client()
        with patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications, "provider", return_value=None), \
                patch.object(main.notifications.requests, "post") as post:
            resp = self._send(client)
        self.assertEqual(resp.status_code, 503)
        self.assertIn("not configured", resp.json()["detail"].lower())
        post.assert_not_called()


class TestSmtpFallback(AlertEndpointsTestCase):
    def test_unreachable_smtp_port_is_503_not_500(self):
        # Reproduces the production symptom: SMTP blocked -> OSError 101.
        client, main = self._client()
        with patch.dict(os.environ, {"SMTP_EMAIL": "me@example.com", "SMTP_APP_PASSWORD": "pw"}), \
                patch.object(main, "_get_alert_context", side_effect=_context), \
                patch.object(main.notifications, "provider", return_value="smtp"), \
                patch.object(main.notifications.smtplib, "SMTP_SSL",
                             side_effect=OSError(101, "Network is unreachable")):
            resp = self._send(client)
        self.assertEqual(resp.status_code, 503)
        self.assertIn("SMTP", resp.json()["detail"])
