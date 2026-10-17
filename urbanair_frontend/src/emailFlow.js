// Recipient check shared by the AlertPanel form (the backend validates again
// before handing the address to SMTP).
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^@\s]+$/;

export function isValidEmail(value) {
  return EMAIL_RE.test(String(value || "").trim());
}

// Pure send-flow helper for the AlertPanel email form.
//
// Extracted so the loading-state / success / failure behaviour can be unit
// tested WITHOUT a DOM (see tests/emailFlow.test.mjs). AlertPanel wires its
// React state setters into this function.
//
// Guarantees:
//   * setSending(true) on entry, setSending(false) in `finally` for BOTH
//     success and failure paths -> the button can never stick on "Sending...".
//   * Success and failure always produce a sendStatus message; a rejected
//     request is NEVER reported as success.
export async function sendEmailFlow({ sendAlert, payload, reportLabel, setSending, setSendStatus }) {
  setSending(true);
  setSendStatus(null);
  try {
    const data = await sendAlert(payload);
    const to = (data && data.to) || payload.to_email;
    setSendStatus({ ok: true, msg: `Sent ${reportLabel} to ${to}` });
    return true;
  } catch (e) {
    const msg = (e && e.message) || String(e) || "Failed to send email";
    setSendStatus({ ok: false, msg });
    return false;
  } finally {
    setSending(false);
  }
}
