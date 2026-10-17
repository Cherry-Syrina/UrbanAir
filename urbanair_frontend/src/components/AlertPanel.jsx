import React, { useEffect, useState } from "react";
import { api } from "../api";
import { sendEmailFlow, isValidEmail } from "../emailFlow";

const REPORTS = [
  { key: "official", label: "Official Report", path: "alertOfficials", field: "report_text" },
  { key: "public", label: "Public Advisory", path: "alertPublic", field: "message" },
];

export default function AlertPanel({ point }) {
  const [alertType, setAlertType] = useState("official");
  const [preview, setPreview] = useState(null);
  const [previewError, setPreviewError] = useState(null);
  const [loadingPreview, setLoadingPreview] = useState(false);

  const [toEmail, setToEmail] = useState("");
  const [touched, setTouched] = useState(false);
  const [smtpConfigured, setSmtpConfigured] = useState(null);

  const [sending, setSending] = useState(false);
  const [sendStatus, setSendStatus] = useState(null);

  const emailValid = isValidEmail(toEmail);
  const report = REPORTS.find((r) => r.key === alertType) || REPORTS[0];

  // Check once whether the server can actually send mail, so the user gets a
  // clear signal instead of a failed send.
  useEffect(() => {
    let alive = true;
    api
      .alertsStatus()
      .then((s) => alive && setSmtpConfigured(!!s.smtp_configured))
      .catch(() => alive && setSmtpConfigured(null));
    return () => {
      alive = false;
    };
  }, []);

  const loadPreview = async (type) => {
    const target = REPORTS.find((r) => r.key === type) || REPORTS[0];
    setAlertType(target.key);
    setPreview(null);
    setPreviewError(null);
    setSendStatus(null);
    setLoadingPreview(true);
    try {
      const data = await api[target.path](point);
      setPreview(data[target.field]);
    } catch (e) {
      setPreviewError(e.message || String(e));
    } finally {
      setLoadingPreview(false);
    }
  };

  const handleSend = async () => {
    setTouched(true);
    // Guard against invalid recipients AND duplicate submissions while a
    // request is already pending (`sending` stays true until finally()).
    if (!emailValid || sending) return;
    await sendEmailFlow({
      sendAlert: api.sendAlert,
      payload: { point, alert_type: alertType, to_email: toEmail.trim() },
      reportLabel: report.label,
      setSending,
      setSendStatus,
    });
  };

  return (
    <div
      style={{
        marginTop: 14,
        paddingTop: 14,
        borderTop: "1px solid var(--border-subtle)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: 10,
          marginBottom: 10,
          flexWrap: "wrap",
        }}
      >
        <strong style={{ fontSize: 12.5 }}>Email notification</strong>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--text-tertiary)" }}>
          Area: {point}
        </span>
      </div>

      {smtpConfigured === false && (
        <div
          style={{
            marginBottom: 10,
            padding: "6px 10px",
            borderRadius: 6,
            fontSize: 11,
            color: "var(--aqi-moderate)",
            background: "var(--bg-base)",
            border: "1px solid var(--border-subtle)",
          }}
        >
          Email sending isn't configured on the server yet (SMTP credentials missing) — sending will
          fail until it is.
        </div>
      )}

      <div style={{ display: "flex", gap: 8, marginBottom: 10, flexWrap: "wrap" }}>
        {REPORTS.map((r) => {
          const active = r.key === alertType;
          return (
            <button
              key={r.key}
              onClick={() => loadPreview(r.key)}
              disabled={loadingPreview}
              style={{
                background: active ? "var(--accent)" : "var(--bg-panel-raised)",
                color: active ? "var(--bg-base)" : "var(--text-secondary)",
                border: "1px solid var(--border-strong)",
                borderRadius: 6,
                padding: "5px 12px",
                fontSize: 11,
                cursor: loadingPreview ? "default" : "pointer",
              }}
            >
              Preview {r.label}
            </button>
          );
        })}
      </div>

      {loadingPreview && (
        <div style={{ fontSize: 11, color: "var(--text-tertiary)", marginBottom: 8 }}>
          Loading preview...
        </div>
      )}

      {previewError && (
        <div style={{ fontSize: 11, color: "var(--aqi-poor)", marginBottom: 8 }}>
          Could not load preview: {previewError}
        </div>
      )}

      {preview && (
        <pre
          style={{
            background: "var(--bg-base)",
            border: "1px solid var(--border-subtle)",
            borderRadius: 6,
            padding: 10,
            fontSize: 10.5,
            fontFamily: "var(--font-mono)",
            color: "var(--text-secondary)",
            whiteSpace: "pre-wrap",
            maxHeight: 220,
            overflowY: "auto",
            margin: "0 0 10px 0",
          }}
        >
          {preview}
        </pre>
      )}

      <div style={{ display: "flex", gap: 8, alignItems: "flex-start", flexWrap: "wrap" }}>
        <input
          type="email"
          placeholder="recipient@email.com"
          value={toEmail}
          onChange={(e) => setToEmail(e.target.value)}
          onBlur={() => setTouched(true)}
          aria-label="Recipient email address"
          style={{
            flex: "1 1 220px",
            background: "var(--bg-panel-raised)",
            color: "var(--text-primary)",
            border: `1px solid ${touched && !emailValid ? "var(--aqi-poor)" : "var(--border-strong)"}`,
            borderRadius: 6,
            padding: "7px 10px",
            fontSize: 12,
          }}
        />
        <button
          onClick={handleSend}
          disabled={sending || (touched && !emailValid)}
          style={{
            background: "var(--accent)",
            color: "var(--bg-base)",
            border: "none",
            borderRadius: 6,
            padding: "7px 16px",
            fontSize: 12,
            fontWeight: 600,
            cursor: sending ? "default" : "pointer",
            opacity: sending ? 0.7 : 1,
          }}
        >
          {sending ? "Sending..." : "Send Email"}
        </button>
      </div>

      {touched && !emailValid && (
        <div style={{ fontSize: 11, marginTop: 6, color: "var(--aqi-poor)" }}>
          Enter a valid email address.
        </div>
      )}

      {sendStatus && (
        <div
          style={{
            fontSize: 11,
            marginTop: 6,
            color: sendStatus.ok ? "var(--aqi-good)" : "var(--aqi-poor)",
          }}
        >
          {sendStatus.msg}
        </div>
      )}
    </div>
  );
}
