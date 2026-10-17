"""
Tests for the alert / email-notification endpoints (/api/alerts/*).

SMTP is MOCKED here -- these tests exercise the wiring, recipient validation
and error handling; they do NOT deliver real email. A genuine delivery test
must be run manually against a controlled recipient with SMTP configured.
"""

import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

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
