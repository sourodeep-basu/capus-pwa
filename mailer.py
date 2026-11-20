"""Sends the verification and password-reset emails. The first provider that is configured is used:
  Brevo  : BREVO_API_KEY + MAIL_FROM   (HTTPS API, works on hosts that block SMTP)
  Resend : RESEND_API_KEY + MAIL_FROM  (HTTPS API; needs a verified domain to email other people)
  SMTP   : SMTP_HOST (+ SMTP_PORT, SMTP_USER, SMTP_PASS) + MAIL_FROM
MAIL_FROM is a plain address you have verified with the provider. With none configured, email features switch off."""
import json
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage

from dotenv import load_dotenv

load_dotenv()


def _from():
    return os.environ.get("MAIL_FROM", "").strip()


def provider():
    if os.environ.get("BREVO_API_KEY") and _from():
        return "brevo"
    if os.environ.get("RESEND_API_KEY") and _from():
        return "resend"
    if os.environ.get("SMTP_HOST") and _from():
        return "smtp"
    return ""


def enabled():
    return bool(provider())


def _post(url, headers, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "CampusLife/1", **headers})
    urllib.request.urlopen(req, timeout=15).read()


def send(to, subject, text):
    p = provider()
    if p == "brevo":
        _post("https://api.brevo.com/v3/smtp/email", {"api-key": os.environ["BREVO_API_KEY"]},
              {"sender": {"email": _from(), "name": "Campus Life"}, "to": [{"email": to}],
               "subject": subject, "textContent": text})
    elif p == "resend":
        _post("https://api.resend.com/emails", {"Authorization": "Bearer " + os.environ["RESEND_API_KEY"]},
              {"from": _from(), "to": [to], "subject": subject, "text": text})
    elif p == "smtp":
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = _from(), to, subject
        msg.set_content(text)
        host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
        ctx = ssl.create_default_context()
        server = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) if port == 465 else smtplib.SMTP(host, port, timeout=15)
        with server:
            if port != 465:
                server.starttls(context=ctx)
            if os.environ.get("SMTP_USER"):
                server.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
            server.send_message(msg)
    else:
        raise RuntimeError("Email is not configured")
