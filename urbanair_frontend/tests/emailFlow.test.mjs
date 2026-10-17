// Frontend unit tests (no DOM) for the email send flow and recipient check.
// Run: npm test   (node --test)
//
// These verify the *loading-state cleanup* and success/failure reporting that
// keep "Send Email" from sticking on "Sending..." — they do NOT send email.

import test from "node:test";
import assert from "node:assert/strict";

import { sendEmailFlow, isValidEmail } from "../src/emailFlow.js";

function recorder() {
  const sending = [];
  let status = undefined;
  return {
    sending,
    get status() {
      return status;
    },
    setSending: (v) => sending.push(v),
    setSendStatus: (v) => {
      status = v;
    },
  };
}

const payload = { point: "Delhi", alert_type: "official", to_email: "user@example.com" };

test("success: reports success and always clears the loading state", async () => {
  const r = recorder();
  const ok = await sendEmailFlow({
    sendAlert: async () => ({ to: "user@example.com" }),
    payload,
    reportLabel: "Official Report",
    setSending: r.setSending,
    setSendStatus: r.setSendStatus,
  });

  assert.equal(ok, true);
  assert.equal(r.status.ok, true);
  assert.match(r.status.msg, /Official Report/);
  assert.deepEqual(r.sending, [true, false]); // crucial: reset in finally
});

test("SMTP failure: reports error, never success, and clears loading", async () => {
  const r = recorder();
  const ok = await sendEmailFlow({
    sendAlert: async () => {
      throw new Error("SMTP is not configured");
    },
    payload,
    reportLabel: "Official Report",
    setSending: r.setSending,
    setSendStatus: r.setSendStatus,
  });

  assert.equal(ok, false);
  assert.equal(r.status.ok, false);
  assert.equal(r.status.msg, "SMTP is not configured");
  assert.deepEqual(r.sending, [true, false]);
});

test("timeout: surfaces the timeout message and clears loading", async () => {
  const r = recorder();
  await sendEmailFlow({
    sendAlert: async () => {
      throw new Error("Request timed out. Please try again.");
    },
    payload,
    reportLabel: "Public Advisory",
    setSending: r.setSending,
    setSendStatus: r.setSendStatus,
  });

  assert.match(r.status.msg, /timed out/i);
  assert.equal(r.status.ok, false);
  assert.equal(r.sending.at(-1), false); // last state is not-sending
});

test("loading is cleared even if the response object is unexpected", async () => {
  const r = recorder();
  await sendEmailFlow({
    sendAlert: async () => null, // e.g. empty body
    payload,
    reportLabel: "Official Report",
    setSending: r.setSending,
    setSendStatus: r.setSendStatus,
  });

  assert.equal(r.status.ok, true);
  assert.match(r.status.msg, /user@example\.com/); // fell back to payload email
  assert.deepEqual(r.sending, [true, false]);
});

test("isValidEmail accepts real addresses and rejects malformed ones", () => {
  for (const good of ["a@b.co", "first.last@sub.domain.com", "x+tag@mail.io"]) {
    assert.equal(isValidEmail(good), true, good);
  }
  for (const bad of ["", "nope", "a@b", "a b@c.com", "@x.com", "x@.com"]) {
    assert.equal(isValidEmail(bad), false, bad);
  }
});
