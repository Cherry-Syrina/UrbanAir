"""
Email Sending
=============
Sends alert emails for the "Send Alert" feature.

Primary provider: **Resend** (https://resend.com) over HTTPS. Hosted
platforms such as Render block outbound SMTP ports (25/465/587) to prevent
spam, so a direct `smtplib.SMTP_SSL(...)` connection fails with
``[Errno 101] Network is unreachable``. An HTTPS API on port 443 always
works from those platforms.

SMTP is kept only as an optional fallback (e.g. for local development or a
self-hosted box that allows outbound SMTP). If both are configured, Resend
wins.

Configuration (environment variables only -- nothing is hardcoded):
  RESEND_API_KEY     -- Resend API key (preferred; enables HTTPS delivery)
  RESEND_FROM_EMAIL  -- verified sender, e.g. "UrbanAir AI <alerts@your.domain>"
  SMTP_EMAIL         -- optional SMTP fallback (e.g. a Gmail address)
  SMTP_APP_PASSWORD  -- optional SMTP fallback (Gmail app password)
  SMTP_HOST / SMTP_PORT -- optional SMTP overrides

A successful return means the provider **accepted** the message for sending;
it does not guarantee inbox delivery (bounces, spam folders and the
recipient's own provider are all outside our control).
"""

import os
import smtplib
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv

load_dotenv()

RESEND_API_URL = "https://api.resend.com/emails"
# Finite (connect, read) timeouts so a hung provider can never pin a worker.
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 15
SMTP_TIMEOUT = 10


class EmailError(Exception):
    """Base class for alert-email failures."""


class EmailNotConfigured(EmailError):
    """No email provider is configured (neither Resend nor SMTP)."""


class EmailTimeout(EmailError):
    """The email provider did not respond within the configured timeout."""


class EmailNetworkError(EmailError):
    """The provider was unreachable (connection failed / network blocked)."""


class EmailProviderError(EmailError):
    """The provider was reachable but refused or failed the send."""

    def __init__(self, message, provider_status=None):
        super().__init__(message)
        self.provider_status = provider_status


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def resend_api_key():
    return _env("RESEND_API_KEY")


def resend_from_email():
    return _env("RESEND_FROM_EMAIL")


def provider():
    """Return the active provider name ("resend" | "smtp") or None."""
    if resend_api_key():
        return "resend"
    if _env("SMTP_EMAIL") and _env("SMTP_APP_PASSWORD"):
        return "smtp"
    return None


def is_configured():
    return provider() is not None


def send_email(to_email, subject, body):
    """Send an email via the configured provider.

    Returns a dict on acceptance by the provider. Raises EmailNotConfigured,
    EmailTimeout or EmailProviderError otherwise. Never logs or returns secrets.
    """
    active = provider()
    if active == "resend":
        return _send_via_resend(to_email, subject, body)
    if active == "smtp":
        return _send_via_smtp(to_email, subject, body)
    raise EmailNotConfigured(
        "Email is not configured. Set RESEND_API_KEY and RESEND_FROM_EMAIL "
        "(recommended) or SMTP_EMAIL and SMTP_APP_PASSWORD."
    )


def _send_via_resend(to_email, subject, body):
    api_key = resend_api_key()
    from_email = resend_from_email()
    if not api_key or not from_email:
        raise EmailNotConfigured(
            "Resend is not fully configured. Set RESEND_API_KEY and RESEND_FROM_EMAIL."
        )

    payload = {"from": from_email, "to": [to_email], "subject": subject, "text": body}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        resp = requests.post(
            RESEND_API_URL,
            json=payload,
            headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except requests.exceptions.Timeout as exc:
        raise EmailTimeout("The email provider timed out. Please try again later.") from exc
    except requests.exceptions.RequestException as exc:
        raise EmailNetworkError("Could not reach the email provider.") from exc

    if resp.status_code >= 400:
        detail = _resend_error_detail(resp)
        message = f"Email provider rejected the request (HTTP {resp.status_code})."
        if detail:
            message = f"{message} {detail}"
        raise EmailProviderError(message, resp.status_code)

    try:
        data = resp.json() or {}
    except ValueError:
        data = {}

    return {"sent": True, "to": to_email, "provider": "resend", "id": data.get("id")}


def _resend_error_detail(resp):
    """Best-effort provider message from an error body (never a secret)."""
    try:
        payload = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:200]
    if isinstance(payload, dict):
        for key in ("message", "error", "name"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _send_via_smtp(to_email, subject, body):
    smtp_email = _env("SMTP_EMAIL")
    smtp_password = _env("SMTP_APP_PASSWORD")
    if not smtp_email or not smtp_password:
        raise EmailNotConfigured(
            "SMTP is not configured (set SMTP_EMAIL and SMTP_APP_PASSWORD)."
        )

    host = _env("SMTP_HOST", "smtp.gmail.com")
    port = int(_env("SMTP_PORT", "465") or "465")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = smtp_email
    msg["To"] = to_email

    try:
        with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT) as server:
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, [to_email], msg.as_string())
    except TimeoutError as exc:
        raise EmailTimeout("SMTP request timed out. Please try again later.") from exc
    except smtplib.SMTPException as exc:
        raise EmailProviderError(f"SMTP send failed: {exc}") from exc
    except OSError as exc:
        # e.g. "[Errno 101] Network is unreachable" -- common on Render because
        # outbound SMTP ports are blocked. Point the operator at Resend instead.
        raise EmailNetworkError(
            "Could not connect to the SMTP server (outbound SMTP is often blocked "
            "on hosted platforms such as Render). Configure RESEND_API_KEY instead."
        ) from exc

    return {"sent": True, "to": to_email, "provider": "smtp"}
