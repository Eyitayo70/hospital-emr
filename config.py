"""
Email + session configuration.

Fill in your real SMTP credentials below to send officer login codes by
email. For Gmail: turn on 2-Step Verification on the account, then create
an "App Password" (Google Account -> Security -> App passwords) and use
that as SMTP_PASSWORD — not your normal Gmail password.

Never commit real credentials to a public git repo. For anything beyond
local testing, set these as environment variables instead of editing
this file directly.
"""
import os

# A long random string used to sign session cookies. Change this to your
# own random value before real use.
SECRET_KEY = os.environ.get("HOSPITAL_SECRET_KEY", "change-this-to-a-random-string")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

# <-- fill these two in -->
SMTP_ADDRESS = os.environ.get("SMTP_ADDRESS", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

# What shows up as the sender name on the email
SENDER_NAME = os.environ.get("SENDER_NAME", "Wayfind General Hospital Registry")
