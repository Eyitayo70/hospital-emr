"""
Hospital Registry — backend API
Flask + SQLite. Serves the static frontend and a JSON API for:
  - Patient hospital folders (registration, search, detail, update)
  - Appointment cards (scheduling, status, listing)
  - A FHIR-lite layer (/fhir/...) so this can plug into a real EHR/EMR
    (Epic, Cerner, OpenMRS, etc.) that speaks HL7 FHIR R4.

Run:  python3 app.py   (serves on http://localhost:5000)
"""
import os
import sqlite3
import datetime
import re
import secrets
import smtplib
from email.mime.text import MIMEText
from functools import wraps
from flask import Flask, request, jsonify, g, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash

import config

DB_PATH = "hospital.db"

app = Flask(__name__, static_folder="public", static_url_path="")
app.secret_key = config.SECRET_KEY
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("RENDER") is not None,
)


# ---------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mrn TEXT UNIQUE NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    dob TEXT NOT NULL,
    gender TEXT,
    phone TEXT,
    email TEXT,
    address TEXT,
    blood_type TEXT,
    allergies TEXT,
    emergency_contact_name TEXT,
    emergency_contact_phone TEXT,
    insurance_provider TEXT,
    insurance_id TEXT,
    department TEXT DEFAULT 'General',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    appointment_code TEXT UNIQUE NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    department TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    location TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ehr_sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('patient', 'officer')),
    full_name TEXT,
    patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS officer_otp (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS password_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    response TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def next_mrn(db):
    year = datetime.datetime.now(datetime.timezone.utc).year
    row = db.execute(
        "SELECT mrn FROM patients WHERE mrn LIKE ? ORDER BY id DESC LIMIT 1",
        (f"HF-{year}-%",),
    ).fetchone()
    seq = int(row["mrn"].split("-")[-1]) + 1 if row else 1
    return f"HF-{year}-{seq:05d}"


def next_appt_code(db):
    year = datetime.datetime.now(datetime.timezone.utc).year
    row = db.execute(
        "SELECT appointment_code FROM appointments WHERE appointment_code LIKE ? ORDER BY id DESC LIMIT 1",
        (f"AP-{year}-%",),
    ).fetchone()
    seq = int(row["appointment_code"].split("-")[-1]) + 1 if row else 1
    return f"AP-{year}-{seq:05d}"


def err(msg, code=400):
    return jsonify({"error": msg}), code


# --------------------------------------------------------------------- auth
def login_required(role):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if session.get("role") != role:
                return err("Not authenticated", 401)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def send_email(to_addr, subject, body):
    """Send an email via SMTP using config.py credentials.
    Falls back to logging the message to the console if SMTP isn't configured,
    so login codes are still visible during local testing."""
    if not config.SMTP_ADDRESS or not config.SMTP_PASSWORD:
        print(f"\n[EMAIL NOT CONFIGURED — printing instead]\nTo: {to_addr}\nSubject: {subject}\n{body}\n")
        return False
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = f"{config.SENDER_NAME} <{config.SMTP_ADDRESS}>"
    msg["To"] = to_addr
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as server:
            if config.SMTP_USE_TLS:
                server.starttls()
            server.login(config.SMTP_ADDRESS, config.SMTP_PASSWORD)
            server.sendmail(config.SMTP_ADDRESS, [to_addr], msg.as_string())
        return True
    except Exception as e:
        print(f"[EMAIL SEND FAILED: {e}] — printing instead]\nTo: {to_addr}\nSubject: {subject}\n{body}\n")
        return False


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    if not session.get("role"):
        return jsonify({"authenticated": False})
    return jsonify({
        "authenticated": True,
        "role": session["role"],
        "email": session.get("email"),
        "full_name": session.get("full_name"),
        "patient_id": session.get("patient_id"),
    })


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/patient/signup", methods=["POST"])
def patient_signup():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    missing = [f for f in ["email", "password", "first_name", "last_name", "dob"] if not data.get(f)]
    if missing:
        return err(f"Missing required fields: {', '.join(missing)}")
    if len(password) < 6:
        return err("Password must be at least 6 characters")
    try:
        datetime.date.fromisoformat(data["dob"])
    except ValueError:
        return err("dob must be in YYYY-MM-DD format")

    db = get_db()
    if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        return err("An account with this email already exists")

    ts = now()
    mrn = next_mrn(db)
    cur = db.execute(
        """INSERT INTO patients
           (mrn, first_name, last_name, dob, gender, phone, email, address,
            blood_type, allergies, emergency_contact_name, emergency_contact_phone,
            insurance_provider, insurance_id, department, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            mrn, data["first_name"], data["last_name"], data["dob"],
            data.get("gender"), data.get("phone"), email, data.get("address"),
            data.get("blood_type"), data.get("allergies"),
            data.get("emergency_contact_name"), data.get("emergency_contact_phone"),
            data.get("insurance_provider"), data.get("insurance_id"),
            data.get("department", "General"), data.get("notes"), ts, ts,
        ),
    )
    patient_id = cur.lastrowid
    db.execute(
        "INSERT INTO users (email, password_hash, role, full_name, patient_id, created_at) VALUES (?,?,?,?,?,?)",
        (email, generate_password_hash(password), "patient", f"{data['first_name']} {data['last_name']}", patient_id, ts),
    )
    db.commit()
    log_sync(db, "out", "Patient", mrn, "created", "Patient self-registered via portal")

    session.clear()
    session["role"] = "patient"
    session["email"] = email
    session["patient_id"] = patient_id
    session["full_name"] = f"{data['first_name']} {data['last_name']}"
    patient = db.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    return jsonify(dict(patient)), 201


@app.route("/api/auth/patient/login", methods=["POST"])
def patient_login():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=? AND role='patient'", (email,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        return err("Incorrect email or password", 401)
    session.clear()
    session["role"] = "patient"
    session["email"] = user["email"]
    session["patient_id"] = user["patient_id"]
    session["full_name"] = user["full_name"]
    return jsonify({"ok": True, "patient_id": user["patient_id"]})


@app.route("/api/auth/officer/signup", methods=["POST"])
def officer_signup():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    full_name = data.get("full_name") or ""
    if not email or not full_name or len(password) < 6:
        return err("Full name, email, and a password of at least 6 characters are required")
    db = get_db()
    if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        return err("An account with this email already exists")
    db.execute(
        "INSERT INTO users (email, password_hash, role, full_name, created_at) VALUES (?,?,?,?,?)",
        (email, generate_password_hash(password), "officer", full_name, now()),
    )
    db.commit()
    return jsonify({"ok": True}), 201


@app.route("/api/auth/officer/request-code", methods=["POST"])
def officer_request_code():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=? AND role='officer'", (email,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        return err("Incorrect email or password", 401)

    code = f"{secrets.randbelow(1000000):06d}"
    expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)).isoformat()
    db.execute(
        "INSERT INTO officer_otp (user_id, code, expires_at, created_at) VALUES (?,?,?,?)",
        (user["id"], code, expires, now()),
    )
    db.commit()
    send_email(
        email, "Your Hospital Registry login code",
        f"Hi {user['full_name']},\n\nYour one-time login code is: {code}\n"
        f"It expires in 10 minutes. If you didn't request this, you can ignore this email.",
    )
    return jsonify({"ok": True, "message": "Code sent to email"})


@app.route("/api/auth/officer/verify-code", methods=["POST"])
def officer_verify_code():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=? AND role='officer'", (email,)).fetchone()
    if not user:
        return err("Incorrect email or code", 401)
    otp = db.execute(
        """SELECT * FROM officer_otp WHERE user_id=? AND code=? AND used=0
           ORDER BY id DESC LIMIT 1""",
        (user["id"], code),
    ).fetchone()
    if not otp:
        return err("Incorrect email or code", 401)
    if datetime.datetime.now(datetime.timezone.utc) > datetime.datetime.fromisoformat(otp["expires_at"]):
        return err("Code expired — request a new one", 401)
    db.execute("UPDATE officer_otp SET used=1 WHERE id=?", (otp["id"],))
    db.commit()
    session.clear()
    session["role"] = "officer"
    session["email"] = user["email"]
    session["full_name"] = user["full_name"]
    return jsonify({"ok": True})


@app.route("/api/auth/request-password-reset", methods=["POST"])
def request_password_reset():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    role = data.get("role")
    if role not in ("patient", "officer"):
        return err("role must be 'patient' or 'officer'")
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=? AND role=?", (email, role)).fetchone()
    # Always return ok, even if not found, so this can't be used to discover
    # which emails have accounts.
    if user:
        code = f"{secrets.randbelow(1000000):06d}"
        expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=15)).isoformat()
        db.execute(
            "INSERT INTO password_resets (user_id, code, expires_at, created_at) VALUES (?,?,?,?)",
            (user["id"], code, expires, now()),
        )
        db.commit()
        send_email(
            email, "Reset your Hospital Registry password",
            f"Hi {user['full_name']},\n\nYour password reset code is: {code}\n"
            f"It expires in 15 minutes. If you didn't request this, you can ignore this email.",
        )
    return jsonify({"ok": True, "message": "If that account exists, a reset code has been sent."})


@app.route("/api/auth/reset-password", methods=["POST"])
def reset_password():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    role = data.get("role")
    code = (data.get("code") or "").strip()
    new_password = data.get("new_password") or ""
    if role not in ("patient", "officer"):
        return err("role must be 'patient' or 'officer'")
    if len(new_password) < 6:
        return err("Password must be at least 6 characters")
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=? AND role=?", (email, role)).fetchone()
    if not user:
        return err("Incorrect email or code", 401)
    reset = db.execute(
        """SELECT * FROM password_resets WHERE user_id=? AND code=? AND used=0
           ORDER BY id DESC LIMIT 1""",
        (user["id"], code),
    ).fetchone()
    if not reset:
        return err("Incorrect email or code", 401)
    if datetime.datetime.now(datetime.timezone.utc) > datetime.datetime.fromisoformat(reset["expires_at"]):
        return err("Code expired — request a new one", 401)
    db.execute("UPDATE password_resets SET used=1 WHERE id=?", (reset["id"],))
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password), user["id"]))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- complaints
@app.route("/api/me/complaints", methods=["GET"])
@login_required("patient")
def my_complaints():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM complaints WHERE patient_id=? ORDER BY created_at DESC", (session["patient_id"],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/me/complaints", methods=["POST"])
@login_required("patient")
def submit_complaint():
    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return err("Complaint message cannot be empty")
    db = get_db()
    ts = now()
    cur = db.execute(
        "INSERT INTO complaints (patient_id, message, status, created_at, updated_at) VALUES (?,?,?,?,?)",
        (session["patient_id"], message, "open", ts, ts),
    )
    db.commit()
    row = db.execute("SELECT * FROM complaints WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/complaints", methods=["GET"])
@login_required("officer")
def list_complaints():
    db = get_db()
    status = request.args.get("status")
    sql = """SELECT c.*, p.first_name, p.last_name, p.mrn FROM complaints c
             JOIN patients p ON p.id = c.patient_id"""
    params = []
    if status:
        sql += " WHERE c.status=?"
        params.append(status)
    sql += " ORDER BY c.created_at DESC"
    rows = db.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/complaints/<int:cid>", methods=["PUT"])
@login_required("officer")
def update_complaint(cid):
    db = get_db()
    existing = db.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
    if not existing:
        return err("Complaint not found", 404)
    data = request.get_json(force=True, silent=True) or {}
    status = data.get("status")
    response = data.get("response")
    if status and status not in ("open", "in-progress", "resolved"):
        return err("Invalid status")
    updates = {}
    if status:
        updates["status"] = status
    if response is not None:
        updates["response"] = response
    if not updates:
        return err("Nothing to update")
    updates["updated_at"] = now()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE complaints SET {set_clause} WHERE id=?", (*updates.values(), cid))
    db.commit()
    row = db.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
    return jsonify(dict(row))


# ------------------------------------------------------------ patient portal
@app.route("/api/me/patient", methods=["GET"])
@login_required("patient")
def my_patient_record():
    db = get_db()
    p = db.execute("SELECT * FROM patients WHERE id=?", (session["patient_id"],)).fetchone()
    if not p:
        return err("Patient record not found", 404)
    appts = db.execute(
        "SELECT * FROM appointments WHERE patient_id=? ORDER BY date, time", (session["patient_id"],)
    ).fetchall()
    result = dict(p)
    result["appointments"] = [dict(a) for a in appts]
    return jsonify(result)


# ------------------------------------------------------------ static pages
@app.route("/")
def home():
    return send_from_directory("public", "home.html")


@app.route("/<path:path>")
def static_proxy(path):
    return send_from_directory("public", path)


# ------------------------------------------------------------------ patients
REQUIRED_PATIENT_FIELDS = ["first_name", "last_name", "dob"]


@app.route("/api/patients", methods=["GET"])
@login_required("officer")
def list_patients():
    db = get_db()
    q = request.args.get("q", "").strip()
    if q:
        like = f"%{q}%"
        rows = db.execute(
            """SELECT * FROM patients
               WHERE first_name LIKE ? OR last_name LIKE ? OR mrn LIKE ? OR phone LIKE ?
               ORDER BY created_at DESC""",
            (like, like, like, like),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM patients ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/patients", methods=["POST"])
@login_required("officer")
def create_patient():
    data = request.get_json(force=True, silent=True) or {}
    missing = [f for f in REQUIRED_PATIENT_FIELDS if not data.get(f)]
    if missing:
        return err(f"Missing required fields: {', '.join(missing)}")
    try:
        datetime.date.fromisoformat(data["dob"])
    except ValueError:
        return err("dob must be in YYYY-MM-DD format")

    db = get_db()
    mrn = next_mrn(db)
    ts = now()
    cur = db.execute(
        """INSERT INTO patients
           (mrn, first_name, last_name, dob, gender, phone, email, address,
            blood_type, allergies, emergency_contact_name, emergency_contact_phone,
            insurance_provider, insurance_id, department, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            mrn, data["first_name"], data["last_name"], data["dob"],
            data.get("gender"), data.get("phone"), data.get("email"), data.get("address"),
            data.get("blood_type"), data.get("allergies"),
            data.get("emergency_contact_name"), data.get("emergency_contact_phone"),
            data.get("insurance_provider"), data.get("insurance_id"),
            data.get("department", "General"), data.get("notes"), ts, ts,
        ),
    )
    db.commit()
    patient = db.execute("SELECT * FROM patients WHERE id=?", (cur.lastrowid,)).fetchone()
    log_sync(db, "out", "Patient", mrn, "created", "New hospital folder opened")
    return jsonify(dict(patient)), 201


@app.route("/api/patients/<int:pid>", methods=["GET"])
@login_required("officer")
def get_patient(pid):
    db = get_db()
    p = db.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    if not p:
        return err("Patient not found", 404)
    appts = db.execute(
        "SELECT * FROM appointments WHERE patient_id=? ORDER BY date DESC, time DESC", (pid,)
    ).fetchall()
    result = dict(p)
    result["appointments"] = [dict(a) for a in appts]
    return jsonify(result)


@app.route("/api/patients/<int:pid>", methods=["PUT"])
@login_required("officer")
def update_patient(pid):
    db = get_db()
    p = db.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    if not p:
        return err("Patient not found", 404)
    data = request.get_json(force=True, silent=True) or {}
    fields = [
        "first_name", "last_name", "dob", "gender", "phone", "email", "address",
        "blood_type", "allergies", "emergency_contact_name", "emergency_contact_phone",
        "insurance_provider", "insurance_id", "department", "notes",
    ]
    updates = {k: data[k] for k in fields if k in data}
    if not updates:
        return err("No updatable fields provided")
    updates["updated_at"] = now()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE patients SET {set_clause} WHERE id=?", (*updates.values(), pid))
    db.commit()
    updated = db.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    log_sync(db, "out", "Patient", updated["mrn"], "updated", "Folder details updated")
    return jsonify(dict(updated))


# --------------------------------------------------------------- appointments
REQUIRED_APPT_FIELDS = ["patient_id", "date", "time", "department", "provider_name"]


@app.route("/api/appointments", methods=["GET"])
@login_required("officer")
def list_appointments():
    db = get_db()
    patient_id = request.args.get("patient_id")
    status = request.args.get("status")
    date = request.args.get("date")
    sql = """SELECT a.*, p.first_name, p.last_name, p.mrn
             FROM appointments a JOIN patients p ON p.id = a.patient_id WHERE 1=1"""
    params = []
    if patient_id:
        sql += " AND a.patient_id=?"
        params.append(patient_id)
    if status:
        sql += " AND a.status=?"
        params.append(status)
    if date:
        sql += " AND a.date=?"
        params.append(date)
    sql += " ORDER BY a.date, a.time"
    rows = db.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/appointments", methods=["POST"])
@login_required("officer")
def create_appointment():
    data = request.get_json(force=True, silent=True) or {}
    missing = [f for f in REQUIRED_APPT_FIELDS if not data.get(f)]
    if missing:
        return err(f"Missing required fields: {', '.join(missing)}")

    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id=?", (data["patient_id"],)).fetchone()
    if not patient:
        return err("Patient not found for this appointment", 404)
    try:
        datetime.date.fromisoformat(data["date"])
    except ValueError:
        return err("date must be in YYYY-MM-DD format")
    if not re.match(r"^\d{2}:\d{2}$", data["time"]):
        return err("time must be in HH:MM (24h) format")

    code = next_appt_code(db)
    ts = now()
    cur = db.execute(
        """INSERT INTO appointments
           (patient_id, appointment_code, date, time, department, provider_name,
            reason, status, location, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            data["patient_id"], code, data["date"], data["time"], data["department"],
            data["provider_name"], data.get("reason"), data.get("status", "scheduled"),
            data.get("location"), data.get("notes"), ts, ts,
        ),
    )
    db.commit()
    appt = db.execute("SELECT * FROM appointments WHERE id=?", (cur.lastrowid,)).fetchone()
    log_sync(db, "out", "Appointment", code, "created", f"Booked for MRN {patient['mrn']}")
    return jsonify(dict(appt)), 201


@app.route("/api/appointments/<int:aid>", methods=["GET"])
@login_required("officer")
def get_appointment(aid):
    db = get_db()
    row = db.execute(
        """SELECT a.*, p.first_name, p.last_name, p.mrn, p.dob, p.phone
           FROM appointments a JOIN patients p ON p.id = a.patient_id WHERE a.id=?""",
        (aid,),
    ).fetchone()
    if not row:
        return err("Appointment not found", 404)
    return jsonify(dict(row))


@app.route("/api/appointments/<int:aid>", methods=["PUT"])
@login_required("officer")
def update_appointment(aid):
    db = get_db()
    existing = db.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone()
    if not existing:
        return err("Appointment not found", 404)
    data = request.get_json(force=True, silent=True) or {}
    fields = ["date", "time", "department", "provider_name", "reason", "status", "location", "notes"]
    updates = {k: data[k] for k in fields if k in data}
    if updates.get("status") and updates["status"] not in (
        "scheduled", "checked-in", "completed", "cancelled", "no-show"
    ):
        return err("Invalid status")
    if not updates:
        return err("No updatable fields provided")
    updates["updated_at"] = now()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE appointments SET {set_clause} WHERE id=?", (*updates.values(), aid))
    db.commit()
    updated = db.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone()
    log_sync(db, "out", "Appointment", updated["appointment_code"], "updated", "Appointment modified")
    return jsonify(dict(updated))


@app.route("/api/appointments/<int:aid>", methods=["DELETE"])
@login_required("officer")
def delete_appointment(aid):
    db = get_db()
    existing = db.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone()
    if not existing:
        return err("Appointment not found", 404)
    db.execute("DELETE FROM appointments WHERE id=?", (aid,))
    db.commit()
    return jsonify({"deleted": True})


# ------------------------------------------------------------------ dashboard
@app.route("/api/dashboard/stats", methods=["GET"])
@login_required("officer")
def dashboard_stats():
    db = get_db()
    today = datetime.date.today().isoformat()
    total_patients = db.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
    today_appts = db.execute(
        "SELECT COUNT(*) c FROM appointments WHERE date=?", (today,)
    ).fetchone()["c"]
    upcoming = db.execute(
        "SELECT COUNT(*) c FROM appointments WHERE date>? AND status='scheduled'", (today,)
    ).fetchone()["c"]
    departments = db.execute(
        "SELECT department, COUNT(*) c FROM patients GROUP BY department ORDER BY c DESC"
    ).fetchall()
    recent_patients = db.execute(
        "SELECT * FROM patients ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    todays_schedule = db.execute(
        """SELECT a.*, p.first_name, p.last_name, p.mrn FROM appointments a
           JOIN patients p ON p.id=a.patient_id WHERE a.date=? ORDER BY a.time""",
        (today,),
    ).fetchall()
    return jsonify({
        "total_patients": total_patients,
        "today_appointments": today_appts,
        "upcoming_appointments": upcoming,
        "departments": [dict(d) for d in departments],
        "recent_patients": [dict(p) for p in recent_patients],
        "todays_schedule": [dict(a) for a in todays_schedule],
    })


def log_sync(db, direction, resource_type, resource_id, status, detail):
    db.execute(
        "INSERT INTO ehr_sync_log (direction, resource_type, resource_id, status, detail, created_at) VALUES (?,?,?,?,?,?)",
        (direction, resource_type, resource_id, status, detail, now()),
    )
    db.commit()


@app.route("/api/ehr-sync-log", methods=["GET"])
@login_required("officer")
def sync_log():
    db = get_db()
    rows = db.execute("SELECT * FROM ehr_sync_log ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------- FHIR-lite bridge
# Minimal HL7 FHIR R4 shaped resources so this system can be wired into a
# real EHR/EMR (Epic, Cerner, OpenMRS, Medplum, etc.) that consumes FHIR.
def patient_to_fhir(p):
    return {
        "resourceType": "Patient",
        "id": str(p["id"]),
        "identifier": [{"system": "urn:hospital:mrn", "value": p["mrn"]}],
        "name": [{"family": p["last_name"], "given": [p["first_name"]]}],
        "gender": (p["gender"] or "unknown").lower(),
        "birthDate": p["dob"],
        "telecom": [
            t for t in [
                {"system": "phone", "value": p["phone"]} if p["phone"] else None,
                {"system": "email", "value": p["email"]} if p["email"] else None,
            ] if t
        ],
        "address": [{"text": p["address"]}] if p["address"] else [],
    }


def appointment_to_fhir(a):
    start = f"{a['date']}T{a['time']}:00"
    status_map = {
        "scheduled": "booked", "checked-in": "arrived", "completed": "fulfilled",
        "cancelled": "cancelled", "no-show": "noshow",
    }
    return {
        "resourceType": "Appointment",
        "id": str(a["id"]),
        "identifier": [{"system": "urn:hospital:appointment", "value": a["appointment_code"]}],
        "status": status_map.get(a["status"], "booked"),
        "description": a["reason"],
        "start": start,
        "serviceType": [{"text": a["department"]}],
        "participant": [
            {"actor": {"display": a["provider_name"]}, "status": "accepted"},
            {"actor": {"reference": f"Patient/{a['patient_id']}"}, "status": "accepted"},
        ],
    }


@app.route("/fhir/Patient/<int:pid>", methods=["GET"])
def fhir_get_patient(pid):
    db = get_db()
    p = db.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    if not p:
        return err("Patient not found", 404)
    return jsonify(patient_to_fhir(p))


@app.route("/fhir/Patient", methods=["GET"])
def fhir_list_patients():
    db = get_db()
    rows = db.execute("SELECT * FROM patients ORDER BY id").fetchall()
    return jsonify({
        "resourceType": "Bundle", "type": "searchset", "total": len(rows),
        "entry": [{"resource": patient_to_fhir(p)} for p in rows],
    })


@app.route("/fhir/Patient", methods=["POST"])
def fhir_import_patient():
    """Accept an inbound FHIR Patient resource from an external EHR/EMR."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get("resourceType") != "Patient":
        return err("Expected resourceType Patient")
    name = (data.get("name") or [{}])[0]
    given = (name.get("given") or [""])[0]
    family = name.get("family", "")
    dob = data.get("birthDate")
    if not (given and family and dob):
        return err("FHIR Patient must include name.given, name.family, and birthDate")
    phone = next((t["value"] for t in data.get("telecom", []) if t.get("system") == "phone"), None)
    email = next((t["value"] for t in data.get("telecom", []) if t.get("system") == "email"), None)

    db = get_db()
    mrn = next_mrn(db)
    ts = now()
    cur = db.execute(
        """INSERT INTO patients (mrn, first_name, last_name, dob, gender, phone, email,
           department, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (mrn, given, family, dob, data.get("gender"), phone, email, "General", ts, ts),
    )
    db.commit()
    p = db.execute("SELECT * FROM patients WHERE id=?", (cur.lastrowid,)).fetchone()
    log_sync(db, "in", "Patient", mrn, "imported", "Received from external EHR/EMR")
    return jsonify(patient_to_fhir(p)), 201


@app.route("/fhir/Appointment/<int:aid>", methods=["GET"])
def fhir_get_appointment(aid):
    db = get_db()
    a = db.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone()
    if not a:
        return err("Appointment not found", 404)
    return jsonify(appointment_to_fhir(a))


@app.route("/fhir/Appointment", methods=["GET"])
def fhir_list_appointments():
    db = get_db()
    rows = db.execute("SELECT * FROM appointments ORDER BY id").fetchall()
    return jsonify({
        "resourceType": "Bundle", "type": "searchset", "total": len(rows),
        "entry": [{"resource": appointment_to_fhir(a)} for a in rows],
    })


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
