// ---------- API helpers ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

const Api = {
  listPatients: (q) => api(`/api/patients${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  getPatient: (id) => api(`/api/patients/${id}`),
  createPatient: (body) => api("/api/patients", { method: "POST", body: JSON.stringify(body) }),
  updatePatient: (id, body) => api(`/api/patients/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  listAppointments: (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return api(`/api/appointments${qs ? `?${qs}` : ""}`);
  },
  getAppointment: (id) => api(`/api/appointments/${id}`),
  createAppointment: (body) => api("/api/appointments", { method: "POST", body: JSON.stringify(body) }),
  updateAppointment: (id, body) => api(`/api/appointments/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteAppointment: (id) => api(`/api/appointments/${id}`, { method: "DELETE" }),
  stats: () => api("/api/dashboard/stats"),

  // auth
  me: () => api("/api/auth/me"),
  logout: () => api("/api/auth/logout", { method: "POST" }),
  patientSignup: (body) => api("/api/auth/patient/signup", { method: "POST", body: JSON.stringify(body) }),
  patientLogin: (body) => api("/api/auth/patient/login", { method: "POST", body: JSON.stringify(body) }),
  officerSignup: (body) => api("/api/auth/officer/signup", { method: "POST", body: JSON.stringify(body) }),
  officerRequestCode: (body) => api("/api/auth/officer/request-code", { method: "POST", body: JSON.stringify(body) }),
  officerVerifyCode: (body) => api("/api/auth/officer/verify-code", { method: "POST", body: JSON.stringify(body) }),
  requestPasswordReset: (body) => api("/api/auth/request-password-reset", { method: "POST", body: JSON.stringify(body) }),
  resetPassword: (body) => api("/api/auth/reset-password", { method: "POST", body: JSON.stringify(body) }),

  // patient portal
  myPatientRecord: () => api("/api/me/patient"),
  myComplaints: () => api("/api/me/complaints"),
  submitComplaint: (message) => api("/api/me/complaints", { method: "POST", body: JSON.stringify({ message }) }),

  // officer complaint review
  listComplaints: (status) => api(`/api/complaints${status ? `?status=${status}` : ""}`),
  updateComplaint: (id, body) => api(`/api/complaints/${id}`, { method: "PUT", body: JSON.stringify(body) }),
};

// ---------- role guard: call at the top of every protected page ----------
async function requireRole(role) {
  try {
    const me = await Api.me();
    if (!me.authenticated || me.role !== role) {
      location.href = role === "officer" ? "/officer-login.html" : "/patient-login.html";
      return null;
    }
    return me;
  } catch (e) {
    location.href = role === "officer" ? "/officer-login.html" : "/patient-login.html";
    return null;
  }
}

async function doLogout(redirectTo) {
  try { await Api.logout(); } catch (e) {}
  location.href = redirectTo || "/";
}

// ---------- department color coding (folder tab colors) ----------
const DEPT_COLORS = {
  "General": "#C9A567",
  "Cardiology": "#B5482F",
  "Pediatrics": "#3D6E63",
  "Orthopedics": "#6B7A63",
  "Neurology": "#5B5EA6",
  "Radiology": "#2E7D8C",
  "Oncology": "#8C4A6B",
  "Emergency": "#A32424",
  "Maternity": "#C77DA0",
  "Dermatology": "#B08968",
};
function deptColor(dept) {
  return DEPT_COLORS[dept] || "#1E3A5F";
}

// ---------- toast ----------
function toast(msg, isError = false) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = isError ? "error" : "";
  requestAnimationFrame(() => el.classList.add("show"));
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 3200);
}

// ---------- formatting ----------
function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
function fmtTime(hhmm) {
  if (!hhmm) return "—";
  const [h, m] = hhmm.split(":").map(Number);
  const period = h >= 12 ? "PM" : "AM";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${String(m).padStart(2, "0")} ${period}`;
}
function age(dob) {
  const b = new Date(dob);
  const t = new Date();
  let a = t.getFullYear() - b.getFullYear();
  if (t.getMonth() < b.getMonth() || (t.getMonth() === b.getMonth() && t.getDate() < b.getDate())) a--;
  return a;
}
function initials(first, last) {
  return `${(first || "?")[0]}${(last || "?")[0]}`.toUpperCase();
}
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- nav shell ----------
const NAV_ITEMS = [
  { href: "/index.html", label: "Dashboard", key: "dashboard" },
  { href: "/patients.html", label: "Patient Registry", key: "patients" },
  { href: "/register.html", label: "New Folder", key: "register" },
  { href: "/appointments.html", label: "Appointments", key: "appointments" },
  { href: "/complaints.html", label: "Complaints", key: "complaints" },
];

function renderShell(activeKey, title, eyebrow) {
  const nav = NAV_ITEMS.map(
    (item) => `<a class="tab-link ${item.key === activeKey ? "active" : ""}" href="${item.href}">
      <span class="dot"></span>${item.label}
    </a>`
  ).join("");

  document.body.insertAdjacentHTML(
    "afterbegin",
    `<div class="shell">
      <aside class="railnav no-print">
        <div class="brand">
          <img src="/img/logo.svg" alt="" width="34" height="34" style="display:block; margin-bottom:8px;">
          Wayfind General<small>Hospital Registry</small>
        </div>
        <nav>${nav}</nav>
        <div style="margin-top:auto; padding:16px 22px 0;">
          <a class="tab-link" href="javascript:doLogout('/officer-login.html')" style="padding-left:0;">
            <span class="dot"></span>Log out
          </a>
        </div>
      </aside>
      <main class="main" id="main-content">
        <div class="topline">
          <div>
            <div class="eyebrow">${eyebrow || ""}</div>
            <h1>${title}</h1>
          </div>
          <div id="topline-actions"></div>
        </div>
        <div id="page-content"></div>
      </main>
    </div>`
  );
}
