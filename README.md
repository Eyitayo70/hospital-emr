# Wayfind General — Hospital Registry

A working web app for patient hospital folders, appointments, and
appointment cards — with two separate portals and a real backend:

- **Patients** self-register, log in with email + password, and see a
  **read-only** view of their own folder and appointments. They can't
  edit anything, and can submit complaints.
- **Health Officers** log in with email + password **and** a one-time
  code emailed to them, then get full access: create/edit patient
  folders, schedule appointments, and respond to complaints.

## Run it

```bash
pip install -r requirements.txt
python3 app.py
```

Open **http://localhost:5000** — that's the landing page where people
choose "I'm a Patient" or "I'm a Health Officer."

## Set up email for officer login codes

Officers must enter a 6-digit code sent to their email before they can
log in. Open **`config.py`** and fill in:

```python
SMTP_ADDRESS = "youraddress@gmail.com"
SMTP_PASSWORD = "your-app-password"
```

For Gmail: turn on 2-Step Verification, then create an **App Password**
(Google Account → Security → App passwords) and use that — not your
normal password.

**Until you fill this in**, the app still works for testing: the code
is printed to the terminal running `python3 app.py` instead of being
emailed, so you can keep testing without email set up yet. Look for a
block like this in the terminal:
```
[EMAIL NOT CONFIGURED — printing instead]
To: officer@example.com
Your one-time login code is: 123456
```

## How access control works

| | Patients | Health Officers |
|---|---|---|
| Register | `/patient-signup.html` (creates login + folder together) | `/officer-signup.html` |
| Log in | Email + password | Email + password + emailed code |
| View own folder | ✅ read-only | ✅ (and everyone else's) |
| Edit folder details | ❌ (must visit in person) | ✅ |
| View own appointments | ✅ | ✅ (and everyone else's) |
| Create/edit appointments | ❌ | ✅ |
| Submit a complaint | ✅ | — |
| Review/respond to complaints | ❌ | ✅ |

Every officer-only API route checks the session server-side
(`@login_required("officer")`), so this isn't just hidden buttons —
a patient's browser genuinely gets rejected (401) if it tries to call
an officer endpoint directly.

Sessions are cookie-based (Flask's built-in session, signed with
`SECRET_KEY` in `config.py`). Change that key before using this for
anything beyond local testing.

## What's included

- **Landing page** (`/`) — choose Patient or Officer
- **Patient signup/login** (`/patient-signup.html`, `/patient-login.html`)
- **Patient dashboard** (`/patient-dashboard.html`) — read-only folder,
  upcoming/past appointments, complaint form + history
- **Officer signup/login** (`/officer-signup.html`, `/officer-login.html`)
  — two-step: password, then emailed code
- **Officer dashboard** (`/index.html`) — today's schedule, recent folders
- **Patient registry** (`/patients.html`) — search all folders (officer only)
- **New/edit folder** (`/register.html`) — officer registers walk-ins or edits any folder
- **Patient folder detail** (`/patient.html?id=..`) — full record + appointments (officer only)
- **Appointments** (`/appointments.html`) — schedule/filter/update status (officer only)
- **Appointment card** (`/appointment-card.html?id=..`) — printable card (officer only)
- **Complaints review** (`/complaints.html`) — officer responds to patient complaints

## Data model

- `patients` — the hospital folder
- `appointments` — linked to a patient
- `users` — login credentials; `role` is `patient` or `officer`; a
  patient user is linked to exactly one `patients` row
- `officer_otp` — one-time codes for officer login, expire after 10 minutes
- `complaints` — linked to a patient, with a status and officer response
- `ehr_sync_log` — audit trail of FHIR imports/exports

## Connecting to a real EHR/EMR

Unchanged from before — see the `/fhir/Patient` and `/fhir/Appointment`
endpoints in `app.py`. These aren't session-gated since external EHR
systems authenticate differently (typically OAuth2/SMART-on-FHIR); add
an API key check there before exposing this beyond your own network.

## Before using this with real patient data

This is a solid testing foundation, not a compliance-certified system.
Before handling real PHI, add:
- HTTPS everywhere (cookies are not marked `Secure` yet — fine for
  local testing, not for the open internet)
- Rate limiting on login and code-verification endpoints
- Stronger password rules and account lockout after repeated failures
- Full audit logging of every access (not just syncs)
- A HIPAA-compliant hosting environment, a signed BAA with your host,
  and legal/compliance review appropriate to your jurisdiction
