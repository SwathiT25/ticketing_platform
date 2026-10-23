# mlops_service_desk.py  |  MLOps Service Desk: FastAPI + PostgreSQL backend with a React frontend, in one file.
# Setup:  docker compose up -d   |   pip install -r requirements.txt   |   python mlops_service_desk.py   |   open http://127.0.0.1:8000
# Config (.env or environment): DATABASE_URL, SECRET_KEY, APP_URL, ADMIN_EMAILS, ADMIN_PASSWORD, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM.
# Admins: every address in ADMIN_EMAILS (or ADMIN_EMAIL) is made an admin on each start; missing accounts are created
# with ADMIN_PASSWORD (or without a password, to activate with "Create account"). Existing passwords are never reset.
# Roles: user (raises tickets), engineer (works tickets assigned to them), admin (assigns, takes ownership, manages roles).
# Only a ticket's owner (its assignee) can start, hold, resume and close it. Every action is written to ticket_history.
# Deployments end as "Successfully Deployed" (closes the ticket) or "Failed / Bug Fix Required": the owner or an admin
# describes the bugs with screenshots, the ticket waits ON HOLD for the fix, and the requester (or the owner or an admin)
# resubmits it for redeployment, which sends it back to the owner.
# Tools:  python mlops_service_desk.py --test-email you@example.com   |   python mlops_service_desk.py --import-sqlite servicedesk.db
#         python mlops_service_desk.py --ensure-admins   (repairs admin roles from ADMIN_EMAILS without starting the server)
# The React frontend loads React + Babel from cdnjs, so the browser needs internet access.
import os, re, sys, json, hmac, hashlib, base64, time, smtplib, secrets, logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime
from email.message import EmailMessage
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
import uvicorn

def _load_env(path):
    # Minimal .env reader so settings can live in a file next to the app; real environment variables win.
    if not os.path.exists(path): return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("servicedesk")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://servicedesk:servicedesk@127.0.0.1:5434/servicedesk")
SECRET = os.environ.get("SECRET_KEY", "change-me-in-production")
SMTP = all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"))
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8000").rstrip("/")
HOST = os.environ.get("HOST", "127.0.0.1")  # 0.0.0.0 makes the app reachable from other machines on the network
PORT = int(os.environ.get("PORT") or 8000)
ROLES = ["user", "engineer", "admin"]
STAT = ["NEW", "ASSIGNED", "IN PROGRESS", "ON HOLD", "CLOSED"]
ACTIVE = ["ASSIGNED", "IN PROGRESS", "ON HOLD"]
TYPES = ["Deployment Request", "Access Request", "Other"]
PRIOS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
ENVS = ["Development", "Staging", "Production"]
DEPLOY_STATUSES = ["Successfully Deployed"]  # failures go through /fail instead of closing the ticket
ACCESS_TYPES = ["Git Repository", "Model Endpoint", "VPN"]
ACCESS_LEVELS = ["Read only", "Read & Write", "Admin"]
ACCESS_STATUSES = ["Access Granted", "Partially Granted", "Rejected"]
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
BRANCH_RE = re.compile(r"^[\w][\w./-]{0,199}$")  # Git branch names: letters, digits, _ . / - and no spaces
URL_RE = re.compile(r"^https?://\S+$")
DELETED_DOMAIN = "removed.invalid"  # deleted accounts with ticket history get deleted-<id>@removed.invalid; never emailed

def fmt(d): return d.astimezone().strftime("%b %d, %Y %I:%M %p")
def clean_email(e):
    e = (e or "").strip().lower()
    if not EMAIL_RE.match(e): raise HTTPException(400, f"Invalid email address: {e or '(empty)'}")
    return e
def email_list(s): return list(dict.fromkeys(clean_email(x) for x in re.split(r"[,;\s]+", s or "") if x.strip()))
def name_from(e): return " ".join(p.capitalize() for p in re.split(r"[._-]+", e.split("@")[0]) if p) or e
def safe_url(u): return re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", u)

# ---------------- Database (PostgreSQL) ----------------
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False, kwargs={"row_factory": dict_row})
SCHEMA = """
create table if not exists users(
  id serial primary key, name text not null, email text not null unique, pw_hash text,
  role text not null default 'user' check (role in ('user','engineer','admin')),
  created_at timestamptz not null default now(), updated_at timestamptz not null default now());
create table if not exists otps(email text primary key, code text not null, name text, purpose text not null, expires_at timestamptz not null);
create table if not exists tickets(
  id serial primary key,
  rid text generated always as ('MLOPS-' || lpad(id::text, greatest(4, length(id::text)), '0')) stored unique,
  title text not null, description text not null, type text not null,
  priority text not null check (priority in ('LOW','MEDIUM','HIGH','CRITICAL')), env text not null,
  repo text not null default '', pr_url text not null default '',
  status text not null default 'NEW' check (status in ('NEW','ASSIGNED','IN PROGRESS','ON HOLD','CLOSED')),
  requester_id int not null references users(id), assignee_id int references users(id),
  ownership text check (ownership in ('assigned','owned')), assigned_by int references users(id), assigned_at timestamptz,
  cc text not null default '', created_at timestamptz not null default now(), updated_at timestamptz not null default now());
create table if not exists ticket_resolutions(
  ticket_id int primary key references tickets(id) on delete cascade, deploy_status text not null, deployed_at timestamptz not null,
  notes text not null, issues text not null, resolution text not null, deploy_url text not null, version text not null default '',
  environment text not null, closed_by int not null references users(id), closed_at timestamptz not null default now());
create table if not exists comments(
  id serial primary key, ticket_id int not null references tickets(id) on delete cascade,
  author_id int not null references users(id), body text not null, created_at timestamptz not null default now());
create table if not exists ticket_history(
  id serial primary key, ticket_id int not null references tickets(id) on delete cascade, action text not null,
  from_status text, to_status text, actor_id int references users(id), detail text not null default '',
  created_at timestamptz not null default now());
create table if not exists notifications(
  id serial primary key, user_id int not null references users(id) on delete cascade,
  ticket_id int references tickets(id) on delete cascade, message text not null, seen boolean not null default false,
  created_at timestamptz not null default now());
create table if not exists audit_log(
  id serial primary key, actor_id int references users(id), action text not null, target text not null,
  detail text not null default '', created_at timestamptz not null default now());
create table if not exists ticket_images(
  id serial primary key, ticket_id int not null references tickets(id) on delete cascade, name text not null,
  mime text not null, data bytea not null, created_at timestamptz not null default now());
alter table otps add column if not exists attempts int not null default 0;  -- wrong guesses; the code stops working after 5
alter table tickets add column if not exists branch text not null default '';  -- Git branch to deploy (Deployment Request)
alter table tickets add column if not exists access jsonb;  -- Access Request details: type, resource, level, for, until
create table if not exists ticket_files(  -- PDFs the owner attaches when closing an access request (VPN config, setup guide)
  id serial primary key, ticket_id int not null references tickets(id) on delete cascade, name text not null,
  data bytea not null, created_at timestamptz not null default now());
alter table ticket_resolutions add column if not exists vpn_user text not null default '';  -- VPN access only
alter table ticket_resolutions add column if not exists vpn_password text not null default '';
alter table ticket_resolutions add column if not exists api_key text not null default '';  -- Model Endpoint access only
create table if not exists deploy_bugs(  -- a failed deployment's bug report and, once fixed, what was fixed
  id serial primary key, ticket_id int not null references tickets(id) on delete cascade, description text not null,
  reported_by int not null references users(id), reported_at timestamptz not null default now(),
  fix_notes text not null default '', fixed_by int references users(id), fixed_at timestamptz);
alter table ticket_images add column if not exists bug_id int references deploy_bugs(id) on delete cascade;  -- screenshot of a failed deployment
create index if not exists ix_deploy_bugs_ticket on deploy_bugs(ticket_id);
alter table users add column if not exists deleted_at timestamptz;  -- set when an admin deletes an account that still has ticket history
create index if not exists ix_tickets_status on tickets(status);
create index if not exists ix_images_ticket on ticket_images(ticket_id);
create index if not exists ix_files_ticket on ticket_files(ticket_id);
create index if not exists ix_tickets_requester on tickets(requester_id);
create index if not exists ix_tickets_assignee on tickets(assignee_id);
create index if not exists ix_history_ticket on ticket_history(ticket_id);
create index if not exists ix_comments_ticket on comments(ticket_id);
create index if not exists ix_notifications_user on notifications(user_id, seen);
"""
def _row(r): return {k: fmt(v) if isinstance(v, datetime) else v for k, v in r.items()}
def q(sql, a=(), one=False):
    with pool.connection() as c:
        rows = [_row(r) for r in c.execute(sql, a).fetchall()]
    return (rows[0] if rows else None) if one else rows
def run(sql, a=()):
    with pool.connection() as c:
        cur = c.execute(sql, a)
        r = cur.fetchone() if cur.description else None
    return _row(r) if r else None
@contextmanager
def tx():
    # One transaction: commits when the block ends, rolls back if anything (including an HTTPException) is raised
    with pool.connection() as c:
        with c.transaction():
            yield c
DEFAULT_ADMIN_PW = "Admin@123"  # only used to warn when an admin still has this well-known password
def hash_pw(p):
    s = secrets.token_hex(8); return s + "$" + hashlib.pbkdf2_hmac("sha256", p.encode(), s.encode(), 100000).hex()
def check_pw(p, h):
    s, x = h.split("$"); return hmac.compare_digest(x, hashlib.pbkdf2_hmac("sha256", p.encode(), s.encode(), 100000).hex())
def start_db():
    try:
        pool.open(wait=True, timeout=15)
    except Exception as ex:
        log.error("Cannot connect to PostgreSQL at %s (%s). Start it with: docker compose up -d", safe_url(DATABASE_URL), ex)
        raise RuntimeError("PostgreSQL is not reachable") from None
    with tx() as c:
        c.execute(SCHEMA)
        admins = ensure_admins(c)
    log.info("Connected to PostgreSQL at %s", safe_url(DATABASE_URL))
    return admins

def configured_admin_emails():
    # ADMIN_EMAILS (comma-separated) plus the older single ADMIN_EMAIL, trimmed, lowercased, de-duplicated
    raw = re.split(r"[,;\s]+", f"{os.environ.get('ADMIN_EMAILS', '')},{os.environ.get('ADMIN_EMAIL', '')}")
    out = []
    for e in (x.strip().lower() for x in raw if x.strip()):
        if not EMAIL_RE.match(e): log.error("Ignoring invalid admin email in ADMIN_EMAILS/ADMIN_EMAIL: %r", e)
        elif e not in out: out.append(e)
    return out
def ensure_admins(c):
    # Runs on every start (and with --ensure-admins). Every configured email ends up as an admin:
    # existing accounts are promoted, missing ones are created. Existing passwords are never changed,
    # and admins who are not listed are left alone.
    pw = os.environ.get("ADMIN_PASSWORD", "")
    for e in configured_admin_emails():
        x = c.execute("select id, role from users where email=%s for update", (e,)).fetchone()
        if not x:
            # Without ADMIN_PASSWORD the account is created without a password; the owner activates it
            # with "Create account" (a code is emailed to that address) and keeps the admin role.
            c.execute("insert into users(name,email,pw_hash,role) values(%s,%s,%s,'admin')", (name_from(e), e, hash_pw(pw) if pw else None))
            audit(c, None, "ADMIN_CREATED", e, "Created from ADMIN_EMAILS" + ("" if pw else " without a password; activate with Create account"))
            log.info("Created admin account %s%s", e, "" if pw else " (no password yet: activate it with 'Create account')")
        elif x["role"] != "admin":
            c.execute("update users set role='admin', updated_at=now() where id=%s", (x["id"],))
            audit(c, None, "ROLE_CHANGED", e, f"{x['role']} -> admin (listed in ADMIN_EMAILS)")
            log.info("Promoted %s from %s to admin (listed in ADMIN_EMAILS)", e, x["role"])
    admins = c.execute("select email, pw_hash from users where role='admin' order by id").fetchall()
    if not admins:
        log.error("There is no admin account. Set ADMIN_EMAILS in .env and restart, or run: python mlops_service_desk.py --ensure-admins")
    for a in admins:
        if a["pw_hash"] and check_pw(DEFAULT_ADMIN_PW, a["pw_hash"]):
            log.warning("Admin %s still uses the default password %s. Change it now (Settings > Change Password in the app).", a["email"], DEFAULT_ADMIN_PW)
    return [a["email"] for a in admins]

@asynccontextmanager
async def lifespan(app):
    start_db(); yield; mailer.shutdown(wait=True); pool.close()  # let queued emails finish before stopping
app = FastAPI(title="MLOps Service Desk", lifespan=lifespan)

@app.exception_handler(psycopg.OperationalError)
async def db_unavailable(request: Request, ex):
    log.error("Database unavailable on %s: %s", request.url.path, ex)
    return JSONResponse(status_code=503, content={"detail": "The database is unavailable right now. Please try again in a moment."})
@app.exception_handler(psycopg.Error)
async def db_failed(request: Request, ex):
    log.exception("Database error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "A database error occurred, so the change was not saved. Please try again."})

# ---------------- Tokens ----------------
def sign(d, ttl):
    b = base64.urlsafe_b64encode(json.dumps({**d, "exp": time.time() + ttl}).encode()).decode()
    return b + "." + hmac.new(SECRET.encode(), b.encode(), hashlib.sha256).hexdigest()
def unsign(t):
    try:
        b, s = t.split(".")
        if not hmac.compare_digest(s, hmac.new(SECRET.encode(), b.encode(), hashlib.sha256).hexdigest()): raise ValueError
        d = json.loads(base64.urlsafe_b64decode(b))
        if d["exp"] < time.time(): raise ValueError
        return d
    except Exception:
        raise HTTPException(401, "Invalid or expired token")
def user(authorization: Optional[str] = Header(None)):
    d = unsign((authorization or "").replace("Bearer ", ""))
    u = q("select id,name,email,role,pw_hash from users where id=%s", (d.get("uid"),), True) if d.get("p") == "auth" else None
    if not u or not u["pw_hash"]: raise HTTPException(401, "Not authenticated")
    return u
def admin(u=Depends(user)):
    if u["role"] != "admin": raise HTTPException(403, "Admin only")
    return u

# ---------------- Mail / notifications ----------------
def mail(to, subject, body):
    # Returns None when sent, otherwise the reason it was not sent
    print(f"\n--- EMAIL to {to} | {subject}\n{body}\n---", flush=True)
    if not SMTP: return "email is not set up (fill in SMTP_HOST, SMTP_USER and SMTP_PASS in .env, then restart)"
    try:
        m = EmailMessage(); m["To"] = to; m["Subject"] = subject
        m["From"] = os.environ.get("SMTP_FROM") or os.environ["SMTP_USER"]; m.set_content(body)
        host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or 587)
        with (smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)) as s:
            if port != 465: s.starttls()
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(m)
        print(f"Mail sent to {to}", flush=True)
        return None
    except Exception as ex:
        print("Mail failed:", ex, flush=True)
        return str(ex)
mailer = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mail")
def notify(email, ticket_id, msg, subject, wait=False):
    # In-app notification when the address has an account, plus an email either way.
    # Emails go out in the background so a button press doesn't wait on the mail server; wait=True returns the send error.
    if email.endswith("@" + DELETED_DOMAIN): return None
    x = q("select id from users where email=%s", (email,), True)
    if x: run("insert into notifications(user_id,ticket_id,message) values(%s,%s,%s)", (x["id"], ticket_id, msg))
    if wait: return mail(email, subject, msg)
    mailer.submit(mail, email, subject, msg)
def admin_emails(): return [a["email"] for a in q("select email from users where role='admin' and pw_hash is not null")]
OTP_TRIES, OTP_GAP_S = 5, 30
def send_otp(email, name, purpose):
    # A code was sent less than 30 s ago: don't let anyone flood an inbox with codes
    if q("select 1 from otps where email=%s and expires_at > now() + make_interval(secs => %s)", (email, 600 - OTP_GAP_S), True):
        raise HTTPException(429, f"A code was just sent. Please wait {OTP_GAP_S} seconds before asking for another.")
    code = f"{secrets.randbelow(1000000):06d}"
    run("""insert into otps(email,code,name,purpose,expires_at) values(%s,%s,%s,%s,now()+interval '10 minutes')
           on conflict (email) do update set code=excluded.code, name=excluded.name, purpose=excluded.purpose,
             expires_at=excluded.expires_at, attempts=0""",
        (email, code, name, purpose))
    err = mail(email, "Your verification code", f"Your 6-digit verification code is {code}. It expires in 10 minutes.")
    if SMTP and err: raise HTTPException(502, f"Could not send the verification email: {err}")
    return {"ok": True, "dev_code": None if SMTP else code}

# ---------------- Schemas ----------------
class RegIn(BaseModel): name: str; email: str
class EmailIn(BaseModel): email: str
class VerIn(BaseModel): email: str; code: str
class PwIn(BaseModel): ticket: str; password: str
class ChangePwIn(BaseModel): current: str; password: str
class LoginIn(BaseModel): email: str; password: str
class ReqIn(BaseModel):
    title: str; description: str; type: str = "Deployment Request"; priority: str = "MEDIUM"
    env: str = "Production"; repo: str = ""; branch: str = ""; pr_url: str = ""; images: list[dict] = []  # images: [{name, data: "data:image/png;base64,..."}]
    access: dict = {}  # Access Request only: {type, resource, level, for, until}
class FailIn(BaseModel): description: str; images: list[dict] = []
class ResubmitIn(BaseModel): fix_notes: str; branch: str = ""; pr_url: str = ""
class CmtIn(BaseModel): body: str
class AsgIn(BaseModel): email: str; cc: str = ""
class HoldIn(BaseModel): reason: str
class RoleIn(BaseModel): role: str
class CloseIn(BaseModel):
    deploy_status: str; deployed_at: str; notes: str; issues: str = ""; resolution: str; deploy_url: str
    version: str = ""; env: str = "Production"
    vpn_user: str = ""; vpn_password: str = ""; api_key: str = ""; files: list[dict] = []  # files: PDFs [{name, data: "data:application/pdf;base64,..."}]

# ---------------- Auth routes ----------------
@app.post("/api/auth/register")
def register(b: RegIn):
    e, name = clean_email(b.email), b.name.strip()
    if not name: raise HTTPException(400, "Enter your name")
    x = q("select pw_hash from users where email=%s", (e,), True)
    if x and x["pw_hash"]: raise HTTPException(400, "Email already registered. Please login.")
    return send_otp(e, name, "register")
@app.post("/api/auth/forgot")
def forgot(b: EmailIn):
    e = clean_email(b.email); u = q("select name from users where email=%s", (e,), True)
    if not u: raise HTTPException(404, "No account found for that email")
    return send_otp(e, u["name"], "reset")
@app.post("/api/auth/resend")
def resend(b: EmailIn):
    o = q("select * from otps where email=%s", (b.email.strip().lower(),), True)
    if not o: raise HTTPException(400, "Start again")
    return send_otp(o["email"], o["name"], o["purpose"])
@app.post("/api/auth/verify")
def verify(b: VerIn):
    e = b.email.strip().lower()
    o = q("select *, expires_at > now() valid from otps where email=%s", (e,), True)
    if o and o["attempts"] >= OTP_TRIES: raise HTTPException(400, "Too many wrong codes. Use Resend Code to get a new one.")
    if not o or o["code"] != b.code.strip() or not o["valid"]:
        if o: run("update otps set attempts=attempts+1 where email=%s", (e,))  # stops someone guessing all million codes
        raise HTTPException(400, "Invalid or expired code")
    run("delete from otps where email=%s", (e,))
    return {"ticket": sign({"p": "setpw", "email": e, "name": o["name"], "purpose": o["purpose"]}, 900)}
@app.post("/api/auth/set-password")
def set_password(b: PwIn):
    d = unsign(b.ticket)
    if d.get("p") != "setpw": raise HTTPException(401, "Invalid ticket")
    if len(b.password) < 8: raise HTTPException(400, "Password must be at least 8 characters")
    with tx() as c:
        x = c.execute("select id, pw_hash from users where email=%s for update", (d["email"],)).fetchone()
        if d["purpose"] == "reset":
            if not x: raise HTTPException(404, "No account found for that email")
            c.execute("update users set pw_hash=%s, updated_at=now() where id=%s", (hash_pw(b.password), x["id"]))
        elif x and x["pw_hash"]: raise HTTPException(400, "Already registered. Please login.")
        elif x:  # invited when a ticket was assigned to this email; activating keeps their role
            c.execute("update users set pw_hash=%s, name=%s, updated_at=now() where id=%s", (hash_pw(b.password), d["name"], x["id"]))
        else:
            c.execute("insert into users(name,email,pw_hash) values(%s,%s,%s)", (d["name"], d["email"], hash_pw(b.password)))
    return {"ok": True}
@app.post("/api/auth/login")
def login(b: LoginIn):
    u = q("select id,pw_hash from users where email=%s", (b.email.strip().lower(),), True)
    if u and not u["pw_hash"]: raise HTTPException(400, "This account isn't activated yet. Use 'Create account' with this email to set a password.")
    if not u or not check_pw(b.password, u["pw_hash"]): raise HTTPException(400, "Invalid email or password")
    return {"token": sign({"p": "auth", "uid": u["id"]}, 7 * 86400)}
@app.post("/api/auth/change-password")
def change_password(b: ChangePwIn, u=Depends(user)):
    # Signed-in users change their own password; works for accounts without a real mailbox (e.g. admin@example.com)
    if not check_pw(b.current, u["pw_hash"]): raise HTTPException(400, "Current password is wrong")
    if len(b.password) < 8: raise HTTPException(400, "New password must be at least 8 characters")
    if b.password == b.current: raise HTTPException(400, "New password must be different from the current one")
    run("update users set pw_hash=%s, updated_at=now() where id=%s", (hash_pw(b.password), u["id"]))
    return {"ok": True}
@app.get("/api/me")
def me(u=Depends(user)):
    n = q("select count(*) c from notifications where user_id=%s and not seen", (u["id"],), True)["c"]
    last = q("select message from notifications where user_id=%s and not seen order by id desc limit 1", (u["id"],), True)
    return {"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"], "unread": n,
            "latest": last["message"].split("\n")[0] if last else None, "build": BUILD}

# ---------------- Tickets: helpers ----------------
SEL = """select t.*, r.name rname, r.email remail, a.name aname, a.email aemail, ab.name assigned_by_name,
    res.deploy_status, res.deployed_at, res.notes deploy_notes, res.issues, res.resolution, res.deploy_url, res.version,
    res.environment res_env, res.closed_at, cb.name closed_by_name, cb.email closed_by_email,
    exists(select 1 from deploy_bugs db where db.ticket_id=t.id and db.fixed_at is null) fix_open
  from tickets t join users r on r.id=t.requester_id left join users a on a.id=t.assignee_id
  left join users ab on ab.id=t.assigned_by left join ticket_resolutions res on res.ticket_id=t.id
  left join users cb on cb.id=res.closed_by"""
def fetch(rid): return q(SEL + " where t.rid=%s", (rid,), True)
def can_view(t, u):
    return (u["role"] == "admin" or t["requester_id"] == u["id"] or t["assignee_id"] == u["id"]
            or u["email"] in (t["access"] or {}).get("for", "").split(","))  # the people an access request is for
def get_ticket(rid, u):
    t = fetch(rid)
    if not t or not can_view(t, u): raise HTTPException(404, "Ticket not found")
    return t
def lock(c, t):
    # Row lock so two people cannot change the same ticket at the same moment
    return c.execute("select status, assignee_id from tickets where id=%s for update", (t["id"],)).fetchone()
def must_own(cur, t, u):
    if cur["assignee_id"] != u["id"]:
        who = f"{t['aname']} <{t['aemail']}>" if t["assignee_id"] else "nobody yet"
        raise HTTPException(403, f"Only the ticket owner ({who}) can do this."
                                 + (" Use Take Ownership to work on it yourself." if u["role"] == "admin" else ""))
def hist(c, tid, action, actor_id, detail="", frm=None, to=None):
    c.execute("insert into ticket_history(ticket_id,action,from_status,to_status,actor_id,detail) values(%s,%s,%s,%s,%s,%s)",
              (tid, action, frm, to, actor_id, detail))
def audit(c, actor_id, action, target, detail=""):
    c.execute("insert into audit_log(actor_id,action,target,detail) values(%s,%s,%s,%s)", (actor_id, action, target, detail))
def link(rid): return f"{APP_URL}/#/t/{rid}"
def ticket_text(t):
    a = t.get("access")
    extra = (f"Access To: {a['type']}\n" + (f"Server / System: {a['resource']}\n" if a["resource"] else "") + (f"Access Level: {a['level']}\n" if a["level"] else "")) if a else "" if t["type"] == "Other" else (
                 f"Repository: {t['repo'] or '-'}\n" + (f"Branch: {t['branch']}\n" if t["branch"] else "") + f"Pull Request: {t['pr_url'] or '-'}\n")
    return (f"Ticket: {t['rid']}\nTitle: {t['title']}\nType: {t['type']}\nPriority: {t['priority']}\nEnvironment: {t['env']}\n"
            f"{extra}Raised by: {t['rname']} <{t['remail']}>\n"
            f"Created: {t['created_at']}\n\nDescription:\n{t['description']}\n\nOpen ticket: {link(t['rid'])}")
def status_counts(where="true", a=()):
    # Dashboards and list tabs both use this, so the numbers always match the ticket lists
    d = dict.fromkeys(STAT, 0)
    for x in q(f"select t.status, count(*) c from tickets t where {where} group by t.status", a): d[x["status"]] = x["c"]
    d["TOTAL"] = sum(d[s] for s in STAT); d["OPEN"] = d["TOTAL"] - d["CLOSED"]
    return d
def listing(where, a, s):
    rows = q(f"{SEL} where {where} and t.status=%s order by t.id desc", [*a, s]) if s in STAT else q(f"{SEL} where {where} order by t.id desc", a)
    return {"rows": rows, "counts": status_counts(where, a)}
def count(where, a=()): return q(f"select count(*) c from tickets t where {where}", a, True)["c"]
FIX_OPEN = "exists(select 1 from deploy_bugs db where db.ticket_id=t.id and db.fixed_at is null)"
LAST_COMMENTER = "(select m.author_id from comments m where m.ticket_id=t.id order by m.id desc limit 1)"

# ---------------- Tickets: reading ----------------
def resolver(u=Depends(user)):
    if u["role"] not in ("engineer", "admin"): raise HTTPException(403, "MLOps Engineers and admins only")
    return u
@app.get("/api/dashboard")
def dashboard(u=Depends(user)):
    uid = u["id"]
    d = {"raised": status_counts("t.requester_id=%s", (uid,)),
         "need_reply": count(f"t.requester_id=%s and t.status<>'CLOSED' and {LAST_COMMENTER}<>%s", (uid, uid)),
         "fix_needed": count(f"t.requester_id=%s and {FIX_OPEN}", (uid,))}
    if u["role"] in ("engineer", "admin"):
        d["owned"] = status_counts("t.assignee_id=%s", (uid,))
        d["owned_need_reply"] = count(f"t.assignee_id=%s and t.status<>'CLOSED' and {LAST_COMMENTER}<>%s", (uid, uid))
        d["closed_by_me"] = q("""select count(*) total, count(*) filter (where r.closed_at >= now() - interval '7 days') week,
                                    extract(epoch from avg(r.closed_at - t.created_at)) avg_s
                                 from ticket_resolutions r join tickets t on t.id=r.ticket_id where r.closed_by=%s""", (uid,), True)
    if u["role"] == "admin":
        d["all"] = status_counts()
        d["kpi"] = q("""select (select count(*) from tickets where created_at >= now() - interval '7 days') created_7d,
                (select count(*) from ticket_resolutions where closed_at >= now() - interval '7 days') closed_7d,
                (select extract(epoch from avg(r.closed_at - t.created_at)) from ticket_resolutions r join tickets t on t.id=r.ticket_id) avg_resolution_s,
                (select count(*) from tickets where status<>'CLOSED' and priority in ('HIGH','CRITICAL')) urgent_open""", (), True)
        d["priority_open"] = dict.fromkeys(PRIOS, 0) | {x["priority"]: x["c"] for x in
                             q("select priority, count(*) c from tickets where status<>'CLOSED' group by priority")}
        d["workload"] = q("""select u.id, u.name, u.email, u.role, (u.pw_hash is not null) active,
                  count(t.id) filter (where t.status='ASSIGNED') assigned, count(t.id) filter (where t.status='IN PROGRESS') in_progress,
                  count(t.id) filter (where t.status='ON HOLD') on_hold,
                  (select count(*) from ticket_resolutions r where r.closed_by=u.id) closed
                from users u left join tickets t on t.assignee_id=u.id and t.status<>'CLOSED'
                where u.role in ('engineer','admin') and u.deleted_at is null group by u.id
                order by count(t.id) desc, u.role='admin', u.name""")
        d["recent"] = q("""select h.id, h.action, h.from_status, h.to_status, h.created_at, x.name actor, t.rid, t.title
                from ticket_history h join tickets t on t.id=h.ticket_id left join users x on x.id=h.actor_id order by h.id desc limit 12""")
        d["users"] = dict.fromkeys(ROLES, 0) | {x["role"]: x["c"] for x in q("select role, count(*) c from users where deleted_at is null group by role")}
        d["users"]["invited"] = q("select count(*) c from users where pw_hash is null and deleted_at is null", (), True)["c"]
    return d
@app.get("/api/requests")
def my_requests(s: str = "ALL", u=Depends(user)): return listing("t.requester_id=%s", [u["id"]], s)
@app.get("/api/assigned")
def assigned(s: str = "ALL", u=Depends(resolver)): return listing("t.assignee_id=%s", [u["id"]], s)
@app.get("/api/queue")
def queue(s: str = "ALL", u=Depends(admin)): return listing("true", [], s)
@app.get("/api/requests/{rid}")
def detail(rid: str, u=Depends(user)):
    t = get_ticket(rid, u)
    hist_rows = q("""select h.*, x.name actor from ticket_history h left join users x on x.id=h.actor_id
                     where h.ticket_id=%s order by h.id""", (t["id"],))
    ts = {}
    for h in hist_rows:
        if h["to_status"]: ts.setdefault(h["to_status"], h["created_at"])
    cm = q("""select m.author_id, m.body, m.created_at, x.name, x.role from comments m join users x on x.id=m.author_id
              where m.ticket_id=%s order by m.id""", (t["id"],))
    imgs = q("select id, name from ticket_images where ticket_id=%s and bug_id is null order by id", (t["id"],))
    bugs = q("""select b.id, b.description, b.reported_at, b.fix_notes, b.fixed_at, x.name reported_by_name, y.name fixed_by_name,
                  coalesce((select json_agg(json_build_object('id', i.id, 'name', i.name) order by i.id) from ticket_images i where i.bug_id=b.id), '[]') images
                from deploy_bugs b join users x on x.id=b.reported_by left join users y on y.id=b.fixed_by where b.ticket_id=%s order by b.id""", (t["id"],))
    files = q("select id, name from ticket_files where ticket_id=%s order by id", (t["id"],))
    creds = q("""select vpn_user, vpn_password, api_key from ticket_resolutions
                 where ticket_id=%s and (vpn_user<>'' or api_key<>'')""", (t["id"],), True)
    # Exact times (epoch seconds) for the tracker's step-to-step durations, plus total time spent ON HOLD
    tse, hold, hold_from = {}, 0.0, None
    for h in q("""select from_status, to_status, extract(epoch from created_at)::float at from ticket_history
                  where ticket_id=%s and to_status is not null order by id""", (t["id"],)):
        tse.setdefault(h["to_status"], h["at"])
        if h["to_status"] == "ON HOLD" and h["from_status"] != "ON HOLD": hold_from = h["at"]
        elif hold_from is not None and h["to_status"] != "ON HOLD": hold += h["at"] - hold_from; hold_from = None
    now = time.time()
    if hold_from is not None: hold += now - hold_from
    return {**t, "history": hist_rows, "comments": cm, "images": imgs, "files": files, "creds": creds, "bugs": bugs, "ts": ts, "tse": tse, "hold_secs": hold, "now": now,
            "is_owner": t["assignee_id"] == u["id"]}
@app.get("/api/requests/{rid}/images/{iid}")
def image(rid: str, iid: int, u=Depends(user)):
    t = get_ticket(rid, u)  # same access rule as the ticket itself
    x = q("select name, mime, data from ticket_images where id=%s and ticket_id=%s", (iid, t["id"]), True)
    if not x: raise HTTPException(404, "Image not found")
    return {"name": x["name"], "src": f"data:{x['mime']};base64," + base64.b64encode(x["data"]).decode()}
@app.get("/api/requests/{rid}/files/{fid}")
def pdf_file(rid: str, fid: int, u=Depends(user)):
    t = get_ticket(rid, u)  # same access rule as the ticket itself
    x = q("select name, data from ticket_files where id=%s and ticket_id=%s", (fid, t["id"]), True)
    if not x: raise HTTPException(404, "File not found")
    return {"name": x["name"], "src": "data:application/pdf;base64," + base64.b64encode(x["data"]).decode()}

# ---------------- Tickets: actions ----------------
MAX_IMAGES, MAX_IMAGE_MB, SHOT_TYPES = 5, 5, ("Other",)  # request types that can attach screenshots
def image_mime(d):
    if d.startswith(b"\x89PNG"): return "image/png"
    if d.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if d.startswith(b"GIF8"): return "image/gif"
    if d[:4] == b"RIFF" and d[8:12] == b"WEBP": return "image/webp"
def parse_images(items):
    if len(items) > MAX_IMAGES: raise HTTPException(400, f"You can attach up to {MAX_IMAGES} images")
    out = []
    for x in items:
        name = str(x.get("name") or "image")[:120]
        try: data = base64.b64decode(str(x.get("data", "")).split(",", 1)[-1], validate=True)
        except Exception: raise HTTPException(400, f"{name} could not be read")
        if len(data) > MAX_IMAGE_MB * 1024 * 1024: raise HTTPException(400, f"{name} is larger than {MAX_IMAGE_MB} MB")
        mime = image_mime(data)
        if not mime: raise HTTPException(400, f"{name} is not a PNG, JPG, GIF or WebP image")
        out.append((name, mime, data))
    return out

MAX_PDFS, MAX_PDF_MB = 5, 10
def parse_pdfs(items):
    if len(items) > MAX_PDFS: raise HTTPException(400, f"You can attach up to {MAX_PDFS} PDF files")
    out = []
    for x in items:
        name = str(x.get("name") or "document.pdf")[:120]
        try: data = base64.b64decode(str(x.get("data", "")).split(",", 1)[-1], validate=True)
        except Exception: raise HTTPException(400, f"{name} could not be read")
        if len(data) > MAX_PDF_MB * 1024 * 1024: raise HTTPException(400, f"{name} is larger than {MAX_PDF_MB} MB")
        if not data.startswith(b"%PDF"): raise HTTPException(400, f"{name} is not a PDF file")
        out.append((name, data))
    return out

def parse_access(a, u):
    kind, res, level = str(a.get("type", "")), str(a.get("resource", "")).strip(), str(a.get("level", ""))
    if kind not in ACCESS_TYPES: raise HTTPException(400, "Choose what you need access to")
    if kind == "Model Endpoint": res, level = "", ""  # the admin sends the endpoint URL and key when closing
    elif not res: raise HTTPException(400, "Enter the repository or VPN you need access to")
    if kind == "VPN": level = ""  # VPN access has no read/write level
    elif kind != "Model Endpoint" and level not in ACCESS_LEVELS: raise HTTPException(400, "Choose an access level")
    # Access is always for the requester; the admin decides how long it lasts
    return {"type": kind, "resource": res[:300], "level": level, "for": u["email"], "until": ""}

@app.post("/api/requests")
def create(b: ReqIn, u=Depends(user)):
    title, desc, repo, pr, branch = b.title.strip(), b.description.strip(), b.repo.strip(), b.pr_url.strip(), b.branch.strip()
    if not title or not desc: raise HTTPException(400, "Title and description are required")
    if len(title) > 200: raise HTTPException(400, "Title must be 200 characters or fewer")
    if b.type not in TYPES or b.priority not in PRIOS or b.env not in ENVS: raise HTTPException(400, "Invalid type, priority or environment")
    access = None
    if b.type == "Access Request":
        repo, pr, access = "", "", parse_access(b.access, u)
    elif b.type == "Other":
        repo, pr = "", ""  # general requests have no repository or pull request
    else:
        if not repo: raise HTTPException(400, "Repository is required")
        if pr and not URL_RE.match(pr): raise HTTPException(400, "Pull Request URL must start with http:// or https://")
    if b.type == "Deployment Request":
        if not branch: raise HTTPException(400, "Branch name is required for a Deployment Request")
        if not BRANCH_RE.match(branch): raise HTTPException(400, "Branch name can only use letters, numbers and _ . / - (no spaces)")
    else:
        branch = ""
    images = parse_images(b.images) if b.type in SHOT_TYPES else []
    with tx() as c:
        t = c.execute("""insert into tickets(title,description,type,priority,env,repo,branch,pr_url,access,requester_id)
                         values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id, rid""",
                      (title, desc, b.type, b.priority, b.env, repo, branch, pr, Jsonb(access) if access else None, u["id"])).fetchone()
        for name, mime, data in images:
            c.execute("insert into ticket_images(ticket_id,name,mime,data) values(%s,%s,%s,%s)", (t["id"], name, mime, data))
        hist(c, t["id"], "CREATED", u["id"], f"Created by {u['name']} <{u['email']}>"
             + (f" with {len(images)} screenshot(s)" if images else ""), None, "NEW")
    t = fetch(t["rid"])
    for e in admin_emails():
        notify(e, t["id"], f"New ticket {t['rid']} is waiting to be assigned.\n\n{ticket_text(t)}", f"[{t['rid']}] New ticket: {title}")
    return {"rid": t["rid"], "status": "NEW"}
@app.post("/api/requests/{rid}/comments")
def comment(rid: str, b: CmtIn, u=Depends(user)):
    t = get_ticket(rid, u); body = b.body.strip()
    if t["status"] == "CLOSED": raise HTTPException(400, "Ticket is closed")
    if not body: raise HTTPException(400, "Message is empty")
    with tx() as c:
        c.execute("insert into comments(ticket_id,author_id,body) values(%s,%s,%s)", (t["id"], u["id"], body))
        hist(c, t["id"], "COMMENTED", u["id"], body if len(body) <= 120 else body[:117] + "...")
    msg = f"{u['name']} replied on {rid}:\n{body}\n\nOpen ticket: {link(rid)}"
    for e in sorted({t["remail"], t["aemail"], *admin_emails()} - {None, u["email"]}):
        notify(e, t["id"], msg, f"[{rid}] New reply")
    return {"ok": True}

def _assign(rid, u, email, cc):
    t = get_ticket(rid, u)
    e = clean_email(email); cc_list = [x for x in email_list(cc) if x != e]
    with tx() as c:
        cur = lock(c, t)
        if cur["status"] == "CLOSED": raise HTTPException(400, "Ticket is already closed")
        tgt = c.execute("select id,name,email,role from users where email=%s for update", (e,)).fetchone()
        if not tgt:  # person without an account: create an invited engineer they can activate with "Create account"
            tgt = c.execute("insert into users(name,email,role) values(%s,%s,'engineer') returning id,name,email,role", (name_from(e), e)).fetchone()
            audit(c, u["id"], "USER_INVITED", e, f"Invited as engineer when {rid} was assigned")
        elif tgt["role"] == "user":
            c.execute("update users set role='engineer', updated_at=now() where id=%s", (tgt["id"],))
            audit(c, u["id"], "ROLE_CHANGED", e, f"user -> engineer (assigned {rid})")
        if cur["assignee_id"] == tgt["id"]: raise HTTPException(400, f"{rid} is already owned by {e}")
        self_take = tgt["id"] == u["id"]
        c.execute("""update tickets set assignee_id=%s, ownership=%s, assigned_by=%s, assigned_at=now(), cc=%s, status='ASSIGNED',
                     updated_at=now() where id=%s""", (tgt["id"], "owned" if self_take else "assigned", u["id"], ",".join(cc_list), t["id"]))
        detail = (f"{u['name']} <{u['email']}> took ownership" if self_take else
                  f"Assigned to {tgt['name']} <{e}> by {u['name']}")
        if t["assignee_id"]: detail += f" (previous owner: {t['aname']} <{t['aemail']}>)"
        if cc_list: detail += "; also notified " + ", ".join(cc_list)
        action = "OWNERSHIP_TAKEN" if self_take else ("REASSIGNED" if cur["assignee_id"] else "ASSIGNED")
        hist(c, t["id"], action, u["id"], detail, cur["status"], "ASSIGNED")
    old, t = t["aemail"], fetch(rid)
    err = None
    if not self_take:
        err = notify(e, t["id"], f"Hi {t['aname']},\n\n{u['name']} has assigned ticket {rid} to you. You are now responsible for "
                     f"resolving and closing it.\n\n{ticket_text(t)}\n\nSign in with {e} to work on it. "
                     f"No account yet? Use 'Create account' with this email.", f"[{rid}] Assigned to you: {t['title']}", wait=True)
    for x in cc_list: notify(x, t["id"], f"Ticket {rid} is now owned by {t['aname']} <{e}>.\n\n{ticket_text(t)}", f"[{rid}] Ticket assigned: {t['title']}")
    if old and old != e: notify(old, t["id"], f"Ticket {rid} is now owned by {t['aname']} <{e}>. You no longer need to work on it.", f"[{rid}] Reassigned")
    if t["remail"] not in (e, u["email"]):
        notify(t["remail"], t["id"], f"{rid} is now owned by {t['aname']}" + (" (MLOps admin)." if self_take else "."), f"[{rid}] Ticket assigned")
    return {"ok": True, "email_error": err}
@app.post("/api/requests/{rid}/assign")
def assign(rid: str, b: AsgIn, u=Depends(admin)): return _assign(rid, u, b.email, b.cc)
@app.post("/api/requests/{rid}/take-ownership")
def take_ownership(rid: str, u=Depends(admin)):
    t = get_ticket(rid, u); return _assign(rid, u, u["email"], t["cc"])

def _transition(rid, u, allowed, to, detail):
    t = get_ticket(rid, u)
    with tx() as c:
        cur = lock(c, t); must_own(cur, t, u)
        if cur["status"] not in allowed: raise HTTPException(400, f"This ticket is {cur['status']}, so it cannot be moved to {to}")
        c.execute("update tickets set status=%s, updated_at=now() where id=%s", (to, t["id"]))
        hist(c, t["id"], "STATUS_CHANGED", u["id"], detail, cur["status"], to)
    if t["remail"] != u["email"]: notify(t["remail"], t["id"], f"{rid} is now {to}: {detail}", f"[{rid}] Status: {to}")
    return {"status": to}
@app.post("/api/requests/{rid}/start")
def start(rid: str, u=Depends(user)): return _transition(rid, u, ["ASSIGNED"], "IN PROGRESS", f"Work started by {u['name']}")
@app.post("/api/requests/{rid}/hold")
def hold(rid: str, b: HoldIn, u=Depends(user)):
    if not b.reason.strip(): raise HTTPException(400, "Give a reason for putting the ticket on hold")
    return _transition(rid, u, ["IN PROGRESS"], "ON HOLD", f"Put on hold by {u['name']}: {b.reason.strip()}")
@app.post("/api/requests/{rid}/resume")
def resume(rid: str, u=Depends(user)):
    if get_ticket(rid, u)["fix_open"]: raise HTTPException(400, "This deployment is waiting for a bug fix. Use Resubmit for Redeployment once it is fixed.")
    return _transition(rid, u, ["ON HOLD"], "IN PROGRESS", f"Work resumed by {u['name']}")
@app.post("/api/requests/{rid}/close")
def close(rid: str, b: CloseIn, u=Depends(user)):
    t = get_ticket(rid, u)
    acc = t["type"] == "Access Request"  # access tickets record the access outcome; the URL is optional
    if b.deploy_status not in (ACCESS_STATUSES if acc else DEPLOY_STATUSES): raise HTTPException(400, "Choose a status")
    if b.env not in ENVS: raise HTTPException(400, "Choose an environment")
    if t["fix_open"]: raise HTTPException(400, "This deployment is waiting for a bug fix. Resubmit it for redeployment first.")
    missing = [n for n, v in (("date/time", b.deployed_at), *((("access given", b.notes),) if acc else (("deployment URL", b.deploy_url),))) if not v.strip()]
    if missing: raise HTTPException(400, "Please fill in: " + ", ".join(missing))
    url = b.deploy_url.strip()
    if url and not URL_RE.match(url): raise HTTPException(400, "URL must start with http:// or https://")
    try: deployed = datetime.fromisoformat(b.deployed_at.strip()).astimezone()  # browser sends local time without a zone
    except ValueError: raise HTTPException(400, "Enter a valid date/time")
    issues = b.issues.strip() or "None"
    vpn = acc and (t["access"] or {}).get("type") == "VPN" and b.deploy_status != "Rejected"
    vuser, vpw = (b.vpn_user.strip(), b.vpn_password.strip()) if vpn else ("", "")
    if vpn and not (vuser and vpw): raise HTTPException(400, "Enter the VPN username and password for the user")
    ep = acc and (t["access"] or {}).get("type") == "Model Endpoint" and b.deploy_status != "Rejected"
    key = b.api_key.strip() if ep else ""
    if ep and not url: raise HTTPException(400, "Enter the model endpoint URL for the user")
    pdfs = parse_pdfs(b.files) if acc else []
    with tx() as c:
        cur = lock(c, t); must_own(cur, t, u)
        if cur["status"] not in ACTIVE: raise HTTPException(400, f"This ticket is {cur['status']}, so it cannot be closed")
        c.execute("""insert into ticket_resolutions(ticket_id,deploy_status,deployed_at,notes,issues,resolution,deploy_url,version,environment,closed_by,vpn_user,vpn_password,api_key)
                     values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (t["id"], b.deploy_status, deployed, b.notes.strip(), issues, b.resolution.strip(), url, b.version.strip(), b.env, u["id"], vuser, vpw, key))
        for name, data in pdfs:
            c.execute("insert into ticket_files(ticket_id,name,data) values(%s,%s,%s)", (t["id"], name, data))
        c.execute("update tickets set status='CLOSED', updated_at=now() where id=%s", (t["id"],))
        hist(c, t["id"], "CLOSED", u["id"], f"Closed by {u['name']} <{u['email']}>. "
             + (f"{b.deploy_status} at {fmt(deployed)}." + (f" {len(pdfs)} PDF(s) attached." if pdfs else "") if acc else f"Deployment {b.deploy_status} at {fmt(deployed)}. URL: {url}"),
             cur["status"], "CLOSED")
    if acc:
        a = t["access"] or {}
        msg = (f"Access request {rid} has been closed by {u['name']} <{u['email']}>.\n\nTitle: {t['title']}\nResult: {b.deploy_status}\n"
               f"Access To: {a.get('type', '-')} - {a.get('resource', '-')}\nAccess Level: {a.get('level') or '-'}\n"
               f"Access For: {a.get('for', '-')}\nCompleted At: {fmt(deployed)}\n" + (f"{'Model Endpoint URL' if ep else 'Link'}: {url}\n" if url else "")
               + (f"VPN Username: {vuser}\nThe VPN password is not sent by email. Sign in and open the ticket to see it.\n" if vpn else "")
               + ("The API key is not sent by email. Sign in and open the ticket to see it.\n" if key else "")
               + (f"Attached PDFs: {', '.join(n for n, _ in pdfs)} (download them from the ticket)\n" if pdfs else "")
               + f"\nAccess Given:\n{b.notes.strip()}\n\nOpen ticket: {link(rid)}")
    else:
        msg = (f"Ticket {rid} has been closed by {u['name']} <{u['email']}>.\n\nTitle: {t['title']}\nDeployment Status: {b.deploy_status}\n"
               f"Deployed At: {fmt(deployed)}\nDeployment URL: {url}\nVersion: {b.version.strip() or '-'}   Environment: {b.env}\n"
               + (f"\nDeployment Notes:\n{b.notes.strip()}\n" if b.notes.strip() else "") + f"\nOpen ticket: {link(rid)}")
    grantees = email_list(t["access"]["for"]) if acc and t["access"] else []  # the people the access is for hear the outcome too
    for e in sorted({t["remail"], *admin_emails(), *email_list(t["cc"]), *grantees} - {None, u["email"]}):
        notify(e, t["id"], msg, f"[{rid}] Ticket closed: {t['title']}")
    return {"ok": True}

# ---------------- Failed deployments: bug report, fix, resubmit ----------------
@app.post("/api/requests/{rid}/fail")
def fail(rid: str, b: FailIn, u=Depends(user)):
    # The owner (or an admin) marks the deployment Failed / Bug Fix Required; it waits ON HOLD until it is resubmitted
    t = get_ticket(rid, u); desc = b.description.strip()
    if t["type"] == "Access Request": raise HTTPException(400, "Access requests cannot be marked as a failed deployment")
    if not desc: raise HTTPException(400, "Describe the bugs or errors you found")
    images = parse_images(b.images)
    with tx() as c:
        cur = lock(c, t)
        if u["role"] != "admin": must_own(cur, t, u)
        if cur["status"] not in ACTIVE: raise HTTPException(400, f"This ticket is {cur['status']}, so it cannot be marked as failed")
        if c.execute("select 1 from deploy_bugs where ticket_id=%s and fixed_at is null", (t["id"],)).fetchone():
            raise HTTPException(400, "This deployment is already waiting for a bug fix")
        bug = c.execute("insert into deploy_bugs(ticket_id,description,reported_by) values(%s,%s,%s) returning id", (t["id"], desc, u["id"])).fetchone()
        for name, mime, data in images:
            c.execute("insert into ticket_images(ticket_id,name,mime,data,bug_id) values(%s,%s,%s,%s,%s)", (t["id"], name, mime, data, bug["id"]))
        c.execute("update tickets set status='ON HOLD', updated_at=now() where id=%s", (t["id"],))
        hist(c, t["id"], "DEPLOY_FAILED", u["id"], f"Failed / Bug Fix Required, reported by {u['name']}"
             + (f" with {len(images)} screenshot(s)" if images else "") + f": {desc if len(desc) <= 120 else desc[:117] + '...'}", cur["status"], "ON HOLD")
    msg = (f"Deployment {rid} ({t['title']}) failed and needs a bug fix.\n\nReported by {u['name']} <{u['email']}>:\n{desc}\n"
           + (f"\n{len(images)} screenshot(s) attached on the ticket.\n" if images else "")
           + f"\nPlease fix the issues, then open the ticket and use Resubmit for Redeployment.\n\nOpen ticket: {link(rid)}")
    for e in sorted({t["remail"], t["aemail"], *admin_emails(), *email_list(t["cc"])} - {None, u["email"]}):
        notify(e, t["id"], msg, f"[{rid}] Deployment failed: bug fix required")
    return {"ok": True}
@app.post("/api/requests/{rid}/resubmit")
def resubmit(rid: str, b: ResubmitIn, u=Depends(user)):
    # After the fix, the requester (or the owner or an admin) sends the deployment back to the owner to redeploy
    t = get_ticket(rid, u); notes, branch, pr = b.fix_notes.strip(), b.branch.strip(), b.pr_url.strip()
    if not (u["role"] == "admin" or u["id"] in (t["requester_id"], t["assignee_id"])):
        raise HTTPException(403, "Only the requester, the owner or an admin can resubmit this deployment")
    if not notes: raise HTTPException(400, "Describe what was fixed")
    if branch and not BRANCH_RE.match(branch): raise HTTPException(400, "Branch name can only use letters, numbers and _ . / - (no spaces)")
    if pr and not URL_RE.match(pr): raise HTTPException(400, "Pull Request URL must start with http:// or https://")
    with tx() as c:
        cur = lock(c, t)
        bug = c.execute("select id from deploy_bugs where ticket_id=%s and fixed_at is null for update", (t["id"],)).fetchone()
        if not bug or cur["status"] == "CLOSED": raise HTTPException(400, "This deployment is not waiting for a bug fix")
        c.execute("update deploy_bugs set fix_notes=%s, fixed_by=%s, fixed_at=now() where id=%s", (notes, u["id"], bug["id"]))
        c.execute("update tickets set status='IN PROGRESS', branch=coalesce(nullif(%s,''),branch), pr_url=coalesce(nullif(%s,''),pr_url), updated_at=now() where id=%s",
                  (branch, pr, t["id"]))
        hist(c, t["id"], "RESUBMITTED", u["id"], f"Fixed and resubmitted for redeployment by {u['name']}"
             + (f" (branch {branch})" if branch and branch != t["branch"] else "") + f": {notes if len(notes) <= 120 else notes[:117] + '...'}",
             cur["status"], "IN PROGRESS")
    msg = (f"The bugs in deployment {rid} ({t['title']}) were fixed by {u['name']} and it is ready to redeploy.\n\nWhat was fixed:\n{notes}\n"
           + (f"Branch: {branch}\n" if branch else "") + (f"Pull Request: {pr}\n" if pr else "")
           + f"\n{t['aname'] or 'The owner'}, please redeploy it and close the ticket.\n\nOpen ticket: {link(rid)}")
    for e in sorted({t["remail"], t["aemail"], *admin_emails(), *email_list(t["cc"])} - {None, u["email"]}):
        notify(e, t["id"], msg, f"[{rid}] Bug fixed: ready to redeploy")
    return {"ok": True}

# ---------------- Users & roles (admin) ----------------
@app.get("/api/users")
def users(u=Depends(admin)):
    return q("""select u.id, u.name, u.email, u.role, (u.pw_hash is not null) active, u.created_at,
                  (select count(*) from tickets t where t.assignee_id=u.id and t.status<>'CLOSED') owned_open
                from users u where u.deleted_at is null order by array_position(array['admin','engineer','user'], u.role), u.name""")

@app.get("/api/team")
def team(u=Depends(admin)):
    # People the admin can pick when assigning: only TEAM_EMAILS from .env. Names come from their account once they sign up.
    emails = email_list(os.environ.get("TEAM_EMAILS", ""))
    known = {x["email"]: x["name"] for x in q("select email, name from users where email = any(%s)", (emails,))}
    return [{"email": e, "name": known.get(e) or name_from(e)} for e in emails]
@app.post("/api/users/{uid}/role")
def set_role(uid: int, b: RoleIn, u=Depends(admin)):
    if b.role not in ROLES: raise HTTPException(400, "Invalid role")
    if uid == u["id"]: raise HTTPException(400, "You cannot change your own role")
    with tx() as c:
        x = c.execute("select id, email, role from users where id=%s and deleted_at is null for update", (uid,)).fetchone()
        if not x: raise HTTPException(404, "User not found")
        if x["role"] == b.role: return {"ok": True}
        if b.role == "user":
            n = c.execute("select count(*) c from tickets where assignee_id=%s and status<>'CLOSED'", (uid,)).fetchone()["c"]
            if n: raise HTTPException(400, f"{x['email']} still owns {n} open ticket(s). Reassign them first.")
        c.execute("update users set role=%s, updated_at=now() where id=%s", (b.role, uid))
        audit(c, u["id"], "ROLE_CHANGED", x["email"], f"{x['role']} -> {b.role}")
    return {"ok": True}

@app.delete("/api/users/{uid}")
def delete_user(uid: int, u=Depends(admin)):
    # Accounts with no tickets, comments or history are removed completely. Accounts with history are anonymised instead:
    # they can no longer sign in, their email is freed (they can register again), and their name stays on old tickets.
    if uid == u["id"]: raise HTTPException(400, "You cannot delete your own account")
    with tx() as c:
        x = c.execute("select id, name, email, role from users where id=%s and deleted_at is null for update", (uid,)).fetchone()
        if not x: raise HTTPException(404, "User not found")
        if x["email"] in configured_admin_emails():
            raise HTTPException(400, f"{x['email']} is listed in ADMIN_EMAILS, so it would be created again on the next restart. Remove it from .env first.")
        n = c.execute("select count(*) c from tickets where assignee_id=%s and status<>'CLOSED'", (uid,)).fetchone()["c"]
        if n: raise HTTPException(400, f"{x['email']} still owns {n} open ticket(s). Reassign them first.")
        try:
            with c.transaction():  # savepoint: a foreign-key error only undoes this delete
                c.execute("delete from users where id=%s", (uid,))
            how = "deleted"
        except psycopg.errors.ForeignKeyViolation:
            c.execute("""update users set name=%s, email=%s, pw_hash=null, role='user', deleted_at=now(), updated_at=now() where id=%s""",
                      (f"{x['name']} (deleted)", f"deleted-{uid}@{DELETED_DOMAIN}", uid))
            c.execute("delete from notifications where user_id=%s", (uid,))
            how = "anonymised"
        audit(c, u["id"], "USER_DELETED", x["email"], f"{x['name']} ({x['role']}) " + ("removed" if how == "deleted" else "deleted; kept on old tickets as a deleted user"))
    return {"ok": True, "how": how}

# ---------------- Notifications ----------------
@app.get("/api/notifications")
def notifs(u=Depends(user)):
    return q("""select n.id, n.message, n.created_at, n.seen, t.rid from notifications n left join tickets t on t.id=n.ticket_id
                where n.user_id=%s order by n.id desc limit 100""", (u["id"],))
@app.post("/api/notifications/read")
def read(u=Depends(user)): run("update notifications set seen=true where user_id=%s and not seen", (u["id"],)); return {"ok": True}

# ---------------- Frontend (React) ----------------
PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MLOps Service Desk</title>
<script>/* apply the saved theme before the page paints, so dark mode never flashes white */
try{var t=localStorage.getItem('theme')||'light';document.documentElement.dataset.theme=(t==='dark'||(t==='system'&&matchMedia('(prefers-color-scheme: dark)').matches))?'dark':'light'}catch(e){}</script>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%234f46e5'/%3E%3Ctext x='16' y='23' font-size='20' text-anchor='middle' fill='white'%3E%E2%9A%99%3C/text%3E%3C/svg%3E">
<script crossorigin src="https://cdnjs.cloudflare.com/ajax/libs/react/18.2.0/umd/react.production.min.js"></script>
<script crossorigin src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.2.0/umd/react-dom.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.23.9/babel.min.js"></script>
<style>
:root{--p:#4f46e5;--p2:#4338ca;--bg:#f1f5f9;--card:#fff;--tx:#0f172a;--mu:#64748b;--bd:#e2e8f0}
*{box-sizing:border-box}
body{margin:0;font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--tx);font-size:14.5px}
a.l{color:var(--p);cursor:pointer;font-weight:600;text-decoration:none;word-break:break-all}a.l:hover{text-decoration:underline}
/* ---------- layout ---------- */
.app{display:flex;min-height:100vh}
.side{width:260px;background:linear-gradient(180deg,#0f172a,#1e1b4b);color:#cbd5e1;display:flex;flex-direction:column;position:fixed;top:0;bottom:0;left:0;z-index:40;padding:20px 14px;transition:transform .25s}
.brand{display:flex;gap:12px;align-items:center;padding:4px 10px 20px;border-bottom:1px solid #ffffff18;margin-bottom:16px}
.logo{height:36px;width:auto;display:block;flex-shrink:0;object-fit:contain}
.brand{cursor:pointer;border-radius:12px}.brand:hover .logo{transform:scale(1.06)}.logo{transition:transform .15s}.brand:focus-visible{outline:2px solid #a5b4fc;outline-offset:2px}
.brand b{color:#fff;display:block;font-size:16px}.brand small{color:#94a3b8}
.menu{flex:1;display:flex;flex-direction:column;gap:6px}
.nl{display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:10px;color:#cbd5e1;cursor:pointer;font-weight:500;font-size:15px;transition:.15s;user-select:none}
.nl:hover{background:#ffffff14;color:#fff}
.nl.on{background:linear-gradient(90deg,#6366f1,#7c3aed);color:#fff;box-shadow:0 6px 16px #4f46e555}
.nl .cnt{margin-left:auto;background:#ef4444;color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px}
.nl.out:hover{background:#ef4444}
.usr{border-top:1px solid #ffffff18;padding:14px 8px;margin-top:10px;display:flex;align-items:center;gap:12px}
.av{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,#f59e0b,#ef4444);color:#fff;display:grid;place-items:center;font-weight:700;flex:none}
.usr .i{min-width:0}.usr b{color:#fff;display:block;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.usr small{color:#94a3b8}
.main{flex:1;margin-left:260px;min-width:0}
.top{position:sticky;top:0;z-index:30;background:#ffffffe6;backdrop-filter:blur(8px);border-bottom:1px solid var(--bd);padding:16px 28px;display:flex;align-items:center;gap:14px}
.top h1{margin:0;font-size:20px}
.burger{display:none;background:#fff;border:1px solid var(--bd);border-radius:10px;width:40px;height:40px;cursor:pointer;place-items:center;color:var(--tx)}
.content{padding:28px;max-width:1180px}
.shade{display:none}
/* ---------- cards / forms ---------- */
.card{background:var(--card);border-radius:14px;padding:22px;box-shadow:0 1px 3px #0f172a14,0 8px 24px #0f172a08;margin-bottom:18px;border:1px solid #eef2f7}
.card h2,.card h3{margin-top:0}
label{display:block;font-size:13px;font-weight:600;color:#334155}
input,select,textarea{width:100%;padding:11px 12px;margin:6px 0 14px;border:1px solid #cbd5e1;border-radius:10px;font:inherit;font-weight:400;background:#fff;color:var(--tx);transition:.15s}
input:focus,select:focus,textarea:focus{outline:0;border-color:var(--p);box-shadow:0 0 0 3px #4f46e522}
.btn{background:var(--p);color:#fff;border:0;padding:11px 20px;border-radius:10px;cursor:pointer;font:inherit;font-weight:600;transition:.15s;display:inline-flex;align-items:center;gap:8px;justify-content:center}
.btn:hover{background:var(--p2);transform:translateY(-1px);box-shadow:0 6px 14px #4f46e544}.btn.g{background:#16a34a}.btn.g:hover{background:#15803d;box-shadow:0 6px 14px #16a34a44}
.btn.ghost{background:#fff;color:var(--tx);border:1px solid var(--bd)}.btn.ghost:hover{background:#f8fafc;box-shadow:none}
.btn.w{width:100%}.btn:disabled{opacity:.55;cursor:not-allowed;transform:none;box-shadow:none}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.section{margin-top:22px;padding-top:18px;border-top:1px solid var(--bd)}
.actions{display:flex;gap:10px;flex-wrap:wrap}
.note{background:#eff6ff;border:1px solid #bfdbfe;color:#1e3a8a;border-radius:12px;padding:12px 14px;margin-top:16px}
.ownbox{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap;background:#faf5ff;border:1px solid #e9d5ff;border-radius:12px;padding:14px 16px}
/* ---------- tables / badges ---------- */
.tw{overflow-x:auto}table{width:100%;border-collapse:collapse}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mu);background:#f8fafc;text-align:left;padding:11px 12px;border-bottom:1px solid var(--bd)}
td{padding:13px 12px;border-bottom:1px solid #f1f5f9;text-align:left;vertical-align:top}tbody tr:hover{background:#f8fafc}
.b{padding:3px 10px;border-radius:99px;font-size:11.5px;font-weight:700;background:#dbeafe;color:#1e40af;white-space:nowrap;display:inline-block}
.b.CLOSED,.b.LOW{background:#dcfce7;color:#166534}.b.ASSIGNED,.b.MEDIUM{background:#fef3c7;color:#92400e}
.b.HIGH,.b.CRITICAL{background:#fee2e2;color:#991b1b}.b.INPROGRESS{background:#e0f2fe;color:#075985}.b.ONHOLD{background:#f1f5f9;color:#475569}
.b.admin{background:#ede9fe;color:#5b21b6}.b.engineer{background:#e0f2fe;color:#075985}.b.user{background:#f1f5f9;color:#475569}
.empty{text-align:center;color:var(--mu);padding:34px 10px}
/* ---------- tabs ---------- */
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px;background:#f1f5f9;padding:5px;border-radius:12px;width:fit-content;max-width:100%}
.tabs a{padding:8px 14px;color:#475569;cursor:pointer;border-radius:9px;font-weight:600;font-size:13.5px}
.tabs a:hover{color:var(--tx)}.tabs a.on{background:#fff;color:var(--p);box-shadow:0 1px 4px #0f172a1a}
/* ---------- stats ---------- */
.hello{margin:0 0 4px;font-size:24px}.sub{color:var(--mu);margin:0 0 22px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px;margin-bottom:22px}
.stat{background:#fff;border-radius:14px;padding:18px;box-shadow:0 1px 3px #0f172a14,0 8px 24px #0f172a08;border:1px solid #eef2f7;display:flex;align-items:center;gap:14px}
.stat .ic{width:48px;height:48px;border-radius:12px;display:grid;place-items:center;flex:none}
.stat small{color:var(--mu);font-weight:600;display:block}.stat h2{margin:2px 0 0;font-size:28px}
.stat.click{cursor:pointer;transition:.15s}.stat.click:hover{transform:translateY(-2px);box-shadow:0 10px 24px #0f172a14;border-color:#c7d2fe}
.ssub{font-size:12px;color:var(--mu);font-weight:400}
.chead{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}.chead h3{margin:0}
.cols{display:grid;grid-template-columns:1.4fr 1fr;gap:18px}.cols>.card{margin-bottom:18px}
.sbar{display:flex;height:16px;border-radius:99px;overflow:hidden;background:#e2e8f0;gap:2px}.sbar i{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:12px;font-size:13px;color:#475569}.legend span{display:flex;align-items:center;gap:6px}
.legend b{width:10px;height:10px;border-radius:3px;display:inline-block}
.hbar{display:grid;grid-template-columns:80px 1fr 36px;gap:10px;align-items:center;margin:8px 0;font-size:13px}.hbar span{font-weight:600;color:#475569}
.hbar div{background:#f1f5f9;border-radius:99px;height:10px;overflow:hidden}.hbar i{display:block;height:100%;border-radius:99px}.hbar b{text-align:right}
.act{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid #f1f5f9}.act:last-child{border-bottom:0}.act small{color:var(--mu);white-space:nowrap}.act a.l{white-space:nowrap}
.note.warn{background:#fffbeb;border-color:#fde68a;color:#92400e}
/* ---------- tracker ---------- */
.track{display:flex;margin:26px 0 12px}
.step{flex:1;text-align:center;font-size:12px;color:#94a3b8;position:relative;padding:0 4px}
.seg{position:absolute;top:9px;left:calc(50% + 20px);width:calc(100% - 40px);height:20px;z-index:2;cursor:pointer;outline:none}
.seg::before{content:'';position:absolute;top:8px;left:0;right:0;height:4px;border-radius:4px;background:#e2e8f0;transition:.15s}
.seg.done::before{background:#16a34a}.seg.now::before{background:#86efac}.seg:hover::before,.seg:focus::before,.seg.open::before{top:6px;height:8px}
.tip{display:none;position:absolute;bottom:24px;left:50%;transform:translateX(-50%);background:#0f172a;color:#fff;padding:8px 12px;border-radius:8px;font-size:12.5px;font-weight:600;white-space:nowrap;box-shadow:0 6px 18px rgba(15,23,42,.25);z-index:5}
.tip small{display:block;font-weight:400;color:#cbd5e1}.seg:hover .tip,.seg:focus .tip,.seg.open .tip{display:block}
.dot{width:36px;height:36px;border-radius:50%;background:#e2e8f0;color:#64748b;margin:0 auto 10px;display:grid;place-items:center;font-weight:700;position:relative;z-index:1;border:3px solid #fff}
.step.done{color:#166534}.step.done .dot{background:#16a34a;color:#fff}.step b{display:block;font-size:12.5px;color:var(--tx)}
.step.cur .dot{background:var(--p);color:#fff;box-shadow:0 0 0 5px #4f46e533}
/* ---------- chat ---------- */
.chat{display:flex;flex-direction:column;gap:10px;margin-bottom:14px;max-height:420px;overflow-y:auto;padding:4px}
.msg{padding:11px 14px;border-radius:14px;max-width:78%;background:#f1f5f9;border-top-left-radius:4px;white-space:pre-wrap}
.msg.me{align-self:flex-end;background:linear-gradient(135deg,#6366f1,#7c3aed);color:#fff;border-top-left-radius:14px;border-top-right-radius:4px}
.msg small{opacity:.75;display:block;margin-bottom:3px}
.chatbox{display:flex;gap:10px;align-items:flex-end}.chatbox textarea{margin:0;flex:1}
.pdfs{list-style:none;padding:0;margin:2px 0 14px;display:flex;flex-direction:column;gap:6px}.pdfs li{display:flex;align-items:center;gap:8px}
.btn.sm{padding:4px 10px;font-size:13px;border-radius:8px}.btn.del{color:#dc2626;border-color:#fecaca}.btn.del:hover{background:#fef2f2}.secret{display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap}.secret code{font-size:14px}
.shots{display:flex;flex-wrap:wrap;gap:10px;margin:2px 0 14px}.shot{position:relative;width:120px;height:90px;border:1px solid #cbd5e1;border-radius:10px;overflow:hidden;background:#f8fafc;display:flex;align-items:center;justify-content:center;color:#64748b;font-size:12px}
.shot img{width:100%;height:100%;object-fit:cover;cursor:zoom-in}.shot button{position:absolute;top:4px;right:4px;width:22px;height:22px;border:0;border-radius:50%;background:rgba(15,23,42,.7);color:#fff;cursor:pointer;line-height:22px;padding:0}
.lightbox{position:fixed;inset:0;z-index:100;background:rgba(15,23,42,.85);display:flex;align-items:center;justify-content:center;padding:24px;cursor:zoom-out}.lightbox img{max-width:100%;max-height:100%;border-radius:8px}
.update{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:90;background:#1e1b4b;color:#fff;padding:12px 16px;border-radius:12px;box-shadow:0 8px 24px rgba(15,23,42,.25);display:flex;gap:12px;align-items:center;max-width:calc(100% - 32px)}
.update button{border:0;border-radius:8px;background:#6366f1;color:#fff;font:inherit;font-weight:600;padding:6px 14px;cursor:pointer}
.nt{display:flex;gap:14px;padding:14px;border-radius:12px;background:#f8fafc;margin-bottom:10px;border:1px solid #eef2f7}
.nt .ic{width:38px;height:38px;border-radius:50%;background:#eef2ff;color:var(--p);display:grid;place-items:center;flex:none}
.b.BUGFIX{background:#fee2e2;color:#991b1b}
.modal{position:fixed;inset:0;z-index:90;background:rgba(15,23,42,.55);display:flex;align-items:center;justify-content:center;padding:16px}
.modal form{background:var(--card);border-radius:16px;padding:22px;width:100%;max-width:560px;max-height:calc(100vh - 32px);overflow:auto;box-shadow:0 20px 50px #0004}
.bugrep{border:1px solid #fecaca;background:#fef2f2;border-radius:12px;padding:12px 14px;margin-top:12px}.bugrep.fixed{border-color:var(--bd);background:transparent}
html[data-theme="dark"] .bugrep{background:#2a1215;border-color:#7f1d1d}html[data-theme="dark"] .bugrep.fixed{background:transparent;border-color:var(--bd)}
.done-box{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;padding:16px;margin-top:16px}
.done-box td{border-color:#dcfce7}.done-box tbody tr:hover{background:transparent}
.kv td{white-space:pre-wrap}.kv td:first-child{width:190px;font-weight:600;color:#475569;white-space:normal}
/* ---------- toast ---------- */
.toast{position:fixed;top:18px;left:50%;transform:translateX(-50%);background:#0f172a;color:#fff;padding:12px 20px;border-radius:12px;z-index:100;box-shadow:0 10px 30px #0f172a55;cursor:pointer;max-width:90%}
/* ---------- auth ---------- */
.authwrap{min-height:100vh;display:grid;grid-template-columns:1.1fr 1fr}
.hero{background:linear-gradient(135deg,#0f172a,#312e81 60%,#6d28d9);color:#fff;padding:60px;display:flex;flex-direction:column;justify-content:center}
.hero .logo{height:64px}.hero h1{font-size:34px;margin:22px 0 10px}.hero p{color:#c7d2fe;font-size:16px;max-width:430px}
.hero ul{list-style:none;padding:0;margin:26px 0 0}.hero li{margin:12px 0;color:#e0e7ff;display:flex;gap:10px;align-items:center}
.hero li span{width:24px;height:24px;border-radius:50%;background:#ffffff22;display:grid;place-items:center}
.formside{display:grid;place-items:center;padding:30px;background:#fff}.formcard{width:100%;max-width:420px}
.formcard h2{font-size:26px;margin:0 0 6px}.formcard .sub{margin-bottom:22px}
.otp{display:flex;gap:10px;justify-content:center;margin-bottom:6px}.otp input{width:50px;height:58px;text-align:center;font-size:24px;font-weight:700;padding:0;margin:0}
.pw{position:relative}.pw input{padding-right:46px}
.eye{position:absolute;right:6px;top:calc(50% - 4px);transform:translateY(-50%);width:34px;height:34px;border:0;border-radius:8px;background:transparent;color:#64748b;cursor:pointer;display:grid;place-items:center;padding:0}
.eye:hover{background:#f1f5f9;color:var(--tx)}
.dev{background:#eef2ff;color:#3730a3;padding:10px 14px;border-radius:10px;margin-bottom:16px}
.meter{height:6px;background:#e2e8f0;border-radius:6px;overflow:hidden;margin:-4px 0 6px}.meter i{display:block;height:100%;transition:.3s}
.ok{width:84px;height:84px;border-radius:50%;background:#dcfce7;color:#16a34a;display:grid;place-items:center;margin:0 auto 14px}
/* ---------- settings ---------- */
.side .nl.set{margin-top:4px}
.seg3{display:inline-flex;gap:4px;background:var(--bg);border:1px solid var(--bd);padding:4px;border-radius:12px;flex-wrap:wrap}
.seg3 button{border:0;background:transparent;color:var(--mu);font:inherit;font-weight:600;padding:9px 16px;border-radius:9px;cursor:pointer;display:inline-flex;gap:8px;align-items:center}
.seg3 button:hover{color:var(--tx)}.seg3 button.on{background:var(--card);color:var(--p);box-shadow:0 1px 4px #0f172a1a}
.seg3 button:focus-visible,.nl:focus-visible{outline:2px solid var(--p);outline-offset:2px}
/* ---------- dark theme (Settings > Appearance; the choice is stored per browser) ---------- */
html[data-theme="dark"]{color-scheme:dark;--p:#6366f1;--p2:#818cf8;--bg:#0b1020;--card:#141a2c;--tx:#e2e8f0;--mu:#94a3b8;--bd:#263049}
html[data-theme="dark"] a.l{color:#a5b4fc}
html[data-theme="dark"] .top{background:#0e1424e6}
html[data-theme="dark"] .burger,html[data-theme="dark"] .btn.ghost{background:var(--card);color:var(--tx)}
html[data-theme="dark"] .btn.ghost:hover{background:#1c2438}
html[data-theme="dark"] .card,html[data-theme="dark"] .stat{background:var(--card);border-color:#1f2740;box-shadow:0 1px 3px #0006}
html[data-theme="dark"] .stat.click:hover{border-color:#4f46e5}
html[data-theme="dark"] .stat .ic{box-shadow:inset 0 0 0 100px #0009}html[data-theme="dark"] .stat .ic svg{filter:brightness(1.9)}
html[data-theme="dark"] label{color:#cbd5e1}
html[data-theme="dark"] input,html[data-theme="dark"] select,html[data-theme="dark"] textarea{background:#0e1424;border-color:#334155;color:var(--tx)}
html[data-theme="dark"] .note{background:#13203f;border-color:#1e3a8a;color:#bfdbfe}
html[data-theme="dark"] .note.warn{background:#2e2108;border-color:#78350f;color:#fcd34d}
html[data-theme="dark"] .ownbox{background:#211a3a;border-color:#4c1d95}
html[data-theme="dark"] th{background:#10172a}html[data-theme="dark"] td{border-color:#1c2439}html[data-theme="dark"] tbody tr:hover{background:#18203a}
html[data-theme="dark"] .b{background:#1e3a8a66;color:#93c5fd}
html[data-theme="dark"] .b.CLOSED,html[data-theme="dark"] .b.LOW{background:#14532d80;color:#86efac}
html[data-theme="dark"] .b.ASSIGNED,html[data-theme="dark"] .b.MEDIUM{background:#78350f80;color:#fcd34d}
html[data-theme="dark"] .b.HIGH,html[data-theme="dark"] .b.CRITICAL{background:#7f1d1d80;color:#fca5a5}
html[data-theme="dark"] .b.INPROGRESS,html[data-theme="dark"] .b.engineer{background:#0c4a6e80;color:#7dd3fc}
html[data-theme="dark"] .b.ONHOLD,html[data-theme="dark"] .b.user{background:#33415580;color:#cbd5e1}
html[data-theme="dark"] .b.admin{background:#4c1d9580;color:#c4b5fd}
html[data-theme="dark"] .tabs{background:#0e1424}html[data-theme="dark"] .tabs a{color:var(--mu)}html[data-theme="dark"] .tabs a.on{background:var(--card);color:#a5b4fc}
html[data-theme="dark"] .seg3 button.on{color:#a5b4fc}
html[data-theme="dark"] .sbar,html[data-theme="dark"] .meter{background:#263049}
html[data-theme="dark"] .legend,html[data-theme="dark"] .hbar span,html[data-theme="dark"] .kv td:first-child{color:var(--mu)}
html[data-theme="dark"] .hbar div{background:#1c2439}html[data-theme="dark"] .act{border-color:#1c2439}
html[data-theme="dark"] .step{color:#64748b}html[data-theme="dark"] .step.done{color:#86efac}
html[data-theme="dark"] .seg::before,html[data-theme="dark"] .dot{background:#263049}html[data-theme="dark"] .dot{color:var(--mu);border-color:var(--card)}
html[data-theme="dark"] .seg.done::before,html[data-theme="dark"] .step.done .dot{background:#16a34a}
html[data-theme="dark"] .tip{background:#020617;border:1px solid #334155}
html[data-theme="dark"] .msg{background:#1c2439}
html[data-theme="dark"] .shot{background:#0e1424;border-color:#334155}
html[data-theme="dark"] .nt{background:#10172a;border-color:#1f2740}html[data-theme="dark"] .nt .ic{background:#1e1b4b;color:#a5b4fc}
html[data-theme="dark"] .done-box{background:#062a19;border-color:#14532d}html[data-theme="dark"] .done-box td{border-color:#14532d}
html[data-theme="dark"] .formside{background:var(--card)}
html[data-theme="dark"] .eye:hover{background:#1c2438}
html[data-theme="dark"] .dev{background:#1e1b4b;color:#c7d2fe}
html[data-theme="dark"] .ok{background:#14532d80;color:#86efac}
html[data-theme="dark"] .toast{background:#020617;border:1px solid #334155}
@media(max-width:960px){
  .side{transform:translateX(-100%)}.side.open{transform:none}.main{margin-left:0}.burger{display:grid}
  .shade.on{display:block;position:fixed;inset:0;background:#0f172a88;z-index:35}
  .cols{grid-template-columns:1fr}.authwrap{grid-template-columns:1fr}.hero{display:none}.content{padding:18px}.top{padding:14px 18px}
  .track{flex-wrap:wrap;row-gap:16px}.step{flex:0 0 50%}.seg{display:none}.msg{max-width:92%}
}
@media(max-width:560px){.row2{grid-template-columns:1fr}.kv td:first-child{width:120px}
  .tw td:first-child{white-space:nowrap}.tw td:first-child+td{min-width:140px}  /* ticket IDs stay on one line; the table scrolls sideways */
  .grid{grid-template-columns:1fr 1fr;gap:10px}.stat{padding:12px;gap:10px}.stat .ic{width:38px;height:38px}.stat h2{font-size:22px}}
</style></head><body><div id="root"></div>
<script type="text/babel">
const {useState,useEffect,useCallback,useRef}=React;
const STAT=["NEW","ASSIGNED","IN PROGRESS","ON HOLD","CLOSED"],TRACK=["NEW","ASSIGNED","IN PROGRESS","CLOSED"],ACTIVE=["ASSIGNED","IN PROGRESS","ON HOLD"];
const DESC={"NEW":"Waiting for assignment","ASSIGNED":"Owner assigned","IN PROGRESS":"Work in progress","ON HOLD":"Paused by the owner","CLOSED":"Resolved and closed"};
const ROLES=["user","engineer","admin"],ROLE_NAME={user:'User',engineer:'MLOps Engineer',admin:'Admin'};
const ACTION={CREATED:'Created',ASSIGNED:'Assigned',REASSIGNED:'Reassigned',OWNERSHIP_TAKEN:'Ownership taken',STATUS_CHANGED:'Status changed',COMMENTED:'Comment',CLOSED:'Closed',
  DEPLOY_FAILED:'Failed / bug fix required',RESUBMITTED:'Resubmitted for redeployment'};
const TYPES=["Deployment Request","Access Request","Other"],PRIOS=["LOW","MEDIUM","HIGH","CRITICAL"],ENVS=["Development","Staging","Production"];
const DEPLOY=["Successfully Deployed","Failed / Bug Fix Required"],FAILED=DEPLOY[1],ACCESS_STAT=["Access Granted","Partially Granted","Rejected"];
const ACCESS_LEVELS=["Read only","Read & Write","Admin"];
// What to ask for in the "server / system" box, per kind of access
const ACCESS_TYPES={"Git Repository":["Repository name or URL","https://github.com/org/ml-model-service"],
  "Model Endpoint":["Model endpoint name or URL","https://ml.example.com/v1/fraud-model"],"VPN":["VPN name or location","Office VPN"]};
// Reads chosen files as data: URLs, the way the API expects uploads
const readFiles=files=>Promise.all(files.map(x=>new Promise((ok,no)=>{const fr=new FileReader();fr.onload=()=>ok({name:x.name,data:fr.result});fr.onerror=no;fr.readAsDataURL(x)})));
const P={dash:'M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z',plus:'M12 5v14M5 12h14',list:'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
bell:'M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0',out:'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9',
queue:'M22 12h-6l-2 3h-4l-2-3H2M5.5 5.1L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.7 4H7.3a2 2 0 0 0-1.8 1.1z',
user:'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',
users:'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75',
pause:'M6 4h4v16H6zM14 4h4v16h-4z',file:'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6',
vol:'M11 5L6 9H2v6h4l5 4V5zM15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14',mute:'M11 5L6 9H2v6h4l5 4V5zM23 9l-6 6M17 9l6 6',
eye:'M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
eyeOff:'M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19M14.12 14.12a3 3 0 1 1-4.24-4.24M1 1l22 22',
key:'M21 2l-2 2m-7.6 7.6a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4',
gear:'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z',
sun:'M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42',
moon:'M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z',
monitor:'M2 3h20v14H2zM8 21h8M12 17v4',
menu:'M3 6h18M3 12h18M3 18h18',check:'M20 6L9 17l-5-5',clock:'M12 6v6l4 2M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z',send:'M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z'};
const Ic=({n,s=19})=><svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d={P[n]}/></svg>;
async function api(path,method='GET',body){
  const t=localStorage.getItem('tok')||sessionStorage.getItem('tok');
  const r=await fetch('/api'+path,{method,headers:{'Content-Type':'application/json',...(t?{Authorization:'Bearer '+t}:{})},body:body?JSON.stringify(body):undefined});
  const d=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(typeof d.detail==='string'?d.detail:'Please check your input');
  return d;
}
const Badge=({v,t})=><span className={'b '+v.replace(' ','')}>{t||v}</span>;
// Status badge for a ticket: a failed deployment waiting for its fix shows BUG FIX REQUIRED instead of ON HOLD
const StatusBadge=({r})=>r.fix_open?<Badge v="BUG FIX" t="BUG FIX REQUIRED"/>:<Badge v={r.status}/>;  // t: display text when it differs from the value (role names)
const Tabs=({items,v,set})=><div className="tabs">{items.map(([k,l])=><a key={k} className={v===k?'on':''} onClick={()=>set(k)}>{l}</a>)}</div>;
const F=({l,...p})=><label>{l}<input {...p}/></label>;
// Password box with an eye button that shows or hides what was typed
function PwField({l,...p}){
  const [show,setShow]=useState(false);
  return <label>{l}<div className="pw"><input {...p} type={show?'text':'password'}/>
    <button type="button" className="eye" onClick={()=>setShow(!show)} title={show?'Hide password':'Show password'} aria-label={show?'Hide password':'Show password'}><Ic n={show?'eyeOff':'eye'} s={18}/></button></div></label>;
}
const Row=({k,children})=><tr><td>{k}</td><td>{children}</td></tr>;
// Screenshot picker shared by the request and bug forms: up to 5 images, 5 MB each
const pickImgs=async(e,imgs,set,flash)=>{const files=[...e.target.files];e.target.value='';
  if(imgs.length+files.length>5)return flash('You can attach up to 5 images');
  const big=files.find(x=>x.size>5*1024*1024);if(big)return flash(`${big.name} is larger than 5 MB`);
  set([...imgs,...await readFiles(files)])};
function ImgPicker({imgs,set,flash}){
  return <><label>Screenshots (optional, up to 5 images, 5 MB each)
    <input type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple onChange={e=>pickImgs(e,imgs,set,flash)}/></label>
    {imgs.length>0&&<div className="shots">{imgs.map((x,i)=><div key={i} className="shot"><img src={x.data} alt={x.name}/>
      <button type="button" title="Remove" onClick={()=>set(imgs.filter((_,j)=>j!==i))}>×</button></div>)}</div>}</>;
}
const Url=({u})=>u?<a className="l" href={u} target="_blank" rel="noreferrer">{u}</a>:'-';
const nowLocal=()=>{const d=new Date();d.setMinutes(d.getMinutes()-d.getTimezoneOffset());return d.toISOString().slice(0,16)};
const owner=r=>r.assignee_id?`${r.aname} <${r.aemail}>`:'Unassigned';
const ownership=r=>r.ownership==='owned'?'Taken by admin':r.ownership==='assigned'?`Assigned by ${r.assigned_by_name}`:'-';
// Notification chime built with Web Audio (no sound file). Browsers only allow audio after the user has clicked or typed once.
let actx=null;
const audio=()=>{try{actx=actx||new (window.AudioContext||window.webkitAudioContext)();if(actx.state==='suspended')actx.resume()}catch{}return actx};
['pointerdown','keydown'].forEach(ev=>addEventListener(ev,audio,{once:true}));
const soundOn=()=>{try{return localStorage.getItem('sound')!=='off'}catch{return true}};
function chime(force){
  if(!force&&!soundOn())return;const c=audio();if(!c)return;
  [[880,0],[1318.5,0.15]].forEach(([f,t])=>{const o=c.createOscillator(),g=c.createGain(),s=c.currentTime+t;o.type='sine';o.frequency.value=f;
    g.gain.setValueAtTime(0.0001,s);g.gain.exponentialRampToValueAtTime(0.3,s+0.02);g.gain.exponentialRampToValueAtTime(0.0001,s+0.5);
    o.connect(g).connect(c.destination);o.start(s);o.stop(s+0.55)});
}

function ReqTable({rows,go,requester,empty='No tickets yet.'}){
  return <div className="tw"><table><thead><tr><th>ID</th><th>Title</th><th>Priority</th><th>Status</th>{requester&&<th>Requester</th>}<th>Owner</th><th>Updated</th></tr></thead><tbody>
    {rows.map(r=><tr key={r.rid}><td><a className="l" onClick={()=>go('detail',r.rid)}>{r.rid}</a></td><td>{r.title}</td><td><Badge v={r.priority}/></td><td><StatusBadge r={r}/></td>
      {requester&&<td>{r.rname}</td>}<td>{r.aname||'Unassigned'}</td><td>{r.updated_at}</td></tr>)}
    {!rows.length&&<tr><td colSpan={requester?7:6} className="empty">{empty}</td></tr>}</tbody></table></div>;
}
// Runs load() now and every 10 s while the tab is visible, so dashboards and lists follow changes made by other people
function useLive(load,deps=[],ms=10000){
  useEffect(()=>{const run=()=>load().catch(()=>{});run();const t=setInterval(()=>{if(!document.hidden)run()},ms);return()=>clearInterval(t)},deps);
}
const LABEL={NEW:'New','ASSIGNED':'Assigned','IN PROGRESS':'In Progress','ON HOLD':'On Hold',CLOSED:'Closed'};
const SC={NEW:['#1d4ed8','#dbeafe','#3b82f6'],ASSIGNED:['#92400e','#fef3c7','#f59e0b'],'IN PROGRESS':['#075985','#e0f2fe','#0ea5e9'],'ON HOLD':['#475569','#f1f5f9','#94a3b8'],CLOSED:['#166534','#dcfce7','#22c55e']};
const SICON={NEW:'plus',ASSIGNED:'user','IN PROGRESS':'clock','ON HOLD':'pause',CLOSED:'check'};
const PC={LOW:'#22c55e',MEDIUM:'#f59e0b',HIGH:'#f97316',CRITICAL:'#ef4444'};
const fmtDur=s=>s==null?'–':s<3600?`${Math.max(1,Math.round(s/60))} min`:s<172800?`${(s/3600).toFixed(1)} h`:`${(s/86400).toFixed(1)} days`;
const Stat=({label,value,icon,color,bg,onClick,sub})=><div className={'stat'+(onClick?' click':'')} onClick={onClick} title={onClick?'Open this list':undefined}>
  <div className="ic" style={{background:bg,color}}><Ic n={icon} s={22}/></div><div><small>{label}</small><h2>{value??'–'}</h2>{sub&&<div className="ssub">{sub}</div>}</div></div>;
function StatusCards({c,pick,total}){
  return <div className="grid"><Stat label={total} value={c.TOTAL} icon="list" color="#4f46e5" bg="#eef2ff" onClick={()=>pick('ALL')}/>
    {STAT.map(s=><Stat key={s} label={LABEL[s]} value={c[s]} icon={SICON[s]} color={SC[s][0]} bg={SC[s][1]} onClick={()=>pick(s)}/>)}</div>;
}
function StatusBar({c}){
  const t=c.TOTAL||0;
  return <div><div className="sbar">{t?STAT.filter(s=>c[s]).map(s=><i key={s} style={{width:(c[s]/t*100)+'%',background:SC[s][2]}} title={`${LABEL[s]}: ${c[s]}`}/>):<i style={{width:'100%',background:'#e2e8f0'}}/>}</div>
    <div className="legend">{STAT.map(s=><span key={s}><b style={{background:SC[s][2]}}/>{LABEL[s]} <strong>{c[s]}</strong> ({t?Math.round(c[s]/t*100):0}%)</span>)}</div></div>;
}
const CardHead=({title,children})=><div className="chead"><h3>{title}</h3>{children}</div>;

/* ---------- Auth screens ---------- */
function AuthShell({children}){
  return <div className="authwrap"><div className="hero"><img className="logo" src="/logo.png" alt="CHAI"/><h1>MLOps Service Desk</h1>
    <p>Raise, track and close deployment, access and bug-fix requests in one place.</p>
    <ul>{['Secure email OTP sign-up','Admin assigns or takes ownership of tickets','Email notifications at every step','Full audit trail for every ticket'].map(t=><li key={t}><span><Ic n="check" s={14}/></span>{t}</li>)}</ul></div>
    <div className="formside"><div className="formcard">{children}</div></div></div>;
}
function Register({go,setReg,flash,reset}){
  const [name,setName]=useState(''),[email,setEmail]=useState(''),[busy,setBusy]=useState(false);
  const submit=async e=>{e.preventDefault();setBusy(true);
    try{const d=await api(reset?'/auth/forgot':'/auth/register','POST',{name,email});setReg({email:email.trim().toLowerCase(),dev:d.dev_code,reset:!!reset});go('verify')}
    catch(x){flash(x.message)}setBusy(false)};
  return <AuthShell><h2>{reset?'Reset Password':'Create Account'}</h2><p className="sub">{reset?'Enter your email to get a verification code.':'Register with your personal or company email. If a ticket was assigned to you, use that email.'}</p>
    <form onSubmit={submit}>{!reset&&<F l="Full Name" value={name} onChange={e=>setName(e.target.value)} required placeholder="John Doe"/>}
    <F l="Email Address" type="email" value={email} onChange={e=>setEmail(e.target.value)} required placeholder="john@example.com"/>
    <button className="btn w" disabled={busy}>Send Verification Code</button></form>
    <p style={{textAlign:'center'}}><a className="l" onClick={()=>go('login')}>Back to login</a></p></AuthShell>;
}
function Verify({go,reg,setReg,flash}){
  const [d,setD]=useState(['','','','','','']);
  const set=(i,v)=>{v=v.replace(/\D/g,'').slice(-1);const n=[...d];n[i]=v;setD(n);if(v&&i<5)document.getElementById('o'+(i+1)).focus()};
  const paste=(i,e)=>{const v=e.clipboardData.getData('text').replace(/\D/g,'');if(!v)return;e.preventDefault();  // pasting the code from the email fills every box
    const n=[...d];[...v.slice(0,6-i)].forEach((x,j)=>n[i+j]=x);setD(n);document.getElementById('o'+Math.min(5,i+v.length)).focus()};
  const submit=async e=>{e.preventDefault();
    try{const r=await api('/auth/verify','POST',{email:reg.email,code:d.join('')});setReg({...reg,ticket:r.ticket});go('password')}catch(x){flash(x.message)}};
  const resend=async()=>{try{const r=await api('/auth/resend','POST',{email:reg.email});setReg({...reg,dev:r.dev_code});flash('A new code has been sent.')}catch(x){flash(x.message)}};
  return <AuthShell><h2>Verify your email</h2><p className="sub">We sent a 6-digit code to <b>{reg.email}</b></p>
    {reg.dev&&<div className="dev">Dev mode (no SMTP): your code is <b>{reg.dev}</b></div>}
    <form onSubmit={submit}><div className="otp">{d.map((v,i)=><input key={i} id={'o'+i} value={v} inputMode="numeric" autoComplete="one-time-code" onChange={e=>set(i,e.target.value)} onPaste={e=>paste(i,e)}
      onKeyDown={e=>{if(e.key==='Backspace'&&!d[i]&&i>0)document.getElementById('o'+(i-1)).focus()}}/>)}</div>
    <p style={{textAlign:'center',color:'var(--mu)'}}>Code expires in 10 minutes.</p><button className="btn w" disabled={d.join('').length<6}>Verify</button></form>
    <p style={{textAlign:'center'}}><a className="l" onClick={resend}>Resend Code</a></p></AuthShell>;
}
function SetPassword({go,reg,flash}){
  const [p,setP]=useState(''),[c,setC]=useState('');
  const n=(p.length>=8)+/[A-Z]/.test(p)+/[0-9]/.test(p)+/[^A-Za-z0-9]/.test(p);
  const col=n>=4?'#16a34a':n>=3?'#65a30d':n>=2?'#d97706':'#dc2626';
  const submit=async e=>{e.preventDefault();
    if(p!==c)return flash('Passwords do not match');
    try{await api('/auth/set-password','POST',{ticket:reg.ticket,password:p});go('login');flash(reg.reset?'Password changed. Please login.':'Account ready. Please login.')}catch(x){flash(x.message)}};
  return <AuthShell><h2>{reg.reset?'New Password':'Set Password'}</h2><p className="sub">Choose a strong password for your account.</p><form onSubmit={submit}>
    <PwField l="Password" value={p} onChange={e=>setP(e.target.value)} required minLength="8" autoComplete="new-password"/>
    <div className="meter"><i style={{width:(p?n*25:0)+'%',background:col}}/></div>
    <div style={{fontWeight:700,marginBottom:12,color:col,minHeight:18}}>{p&&['Weak','Weak','Fair','Good','Strong'][n]}</div>
    <PwField l="Confirm Password" value={c} onChange={e=>setC(e.target.value)} required autoComplete="new-password"/>
    <button className="btn w">{reg.reset?'Change Password':'Create Account'}</button></form></AuthShell>;
}
function Login({go,flash,onLogin}){
  const [email,setEmail]=useState(''),[pw,setPw]=useState(''),[rem,setRem]=useState(true);
  const submit=async e=>{e.preventDefault();
    try{const r=await api('/auth/login','POST',{email,password:pw});localStorage.removeItem('tok');sessionStorage.removeItem('tok');
      (rem?localStorage:sessionStorage).setItem('tok',r.token);onLogin()}catch(x){flash(x.message)}};
  return <AuthShell><h2>Welcome Back</h2><p className="sub">Login to your service desk account.</p><form onSubmit={submit}>
    <F l="Email Address" type="email" value={email} onChange={e=>setEmail(e.target.value)} required placeholder="john@example.com"/>
    <PwField l="Password" value={pw} onChange={e=>setPw(e.target.value)} required autoComplete="current-password"/>
    <div style={{display:'flex',justifyContent:'space-between',marginBottom:16}}>
      <label style={{display:'flex',alignItems:'center',gap:8,fontWeight:500}}><input type="checkbox" style={{width:'auto',margin:0}} checked={rem} onChange={e=>setRem(e.target.checked)}/>Remember me</label>
      <a className="l" onClick={()=>go('forgot')}>Forgot password?</a></div>
    <button className="btn w">Login</button></form>
    <p style={{textAlign:'center'}}>New user? <a className="l" onClick={()=>go('register')}>Create account</a></p></AuthShell>;
}

/* ---------- App pages ---------- */
function UserDash({me,go}){
  const [d,setD]=useState(null),[rows,setRows]=useState([]);
  useLive(()=>Promise.all([api('/dashboard').then(setD),api('/requests?s=ALL').then(x=>setRows(x.rows.slice(0,6)))]));
  if(!d)return <div className="card">Loading…</div>;
  return <div><h2 className="hello">Welcome, {me.name}!</h2><p className="sub">Your requests at a glance. Click a card to open that list. Updates automatically.</p>
    <StatusCards c={d.raised} pick={s=>go('my',s)} total="All My Requests"/>
    {d.fix_needed>0&&<div className="note warn" style={{marginTop:0,marginBottom:18}}><b>{d.fix_needed}</b> of your deployments failed and {d.fix_needed>1?'need':'needs'} a bug fix. Open {d.fix_needed>1?'them':'it'}, fix the issues and use Resubmit for Redeployment.</div>}
    {d.need_reply>0&&<div className="note warn" style={{marginTop:0,marginBottom:18}}><b>{d.need_reply}</b> of your open requests {d.need_reply>1?'have':'has'} a reply waiting for you. Open the ticket's Conversation tab.</div>}
    <div className="card"><CardHead title="Recent Requests"><button className="btn" onClick={()=>go('raise')}><Ic n="plus" s={16}/> Raise Request</button></CardHead>
      <ReqTable rows={rows} go={go} empty="You haven't raised any requests yet."/></div></div>;
}
function EngineerDash({me,go}){
  const [d,setD]=useState(null),[rows,setRows]=useState([]);
  useLive(()=>Promise.all([api('/dashboard').then(setD),api('/assigned?s=ALL').then(x=>setRows(x.rows.filter(r=>r.status!=='CLOSED')))]));
  if(!d)return <div className="card">Loading…</div>;
  const o=d.owned,cb=d.closed_by_me;
  return <div><h2 className="hello">Welcome, {me.name}!</h2><p className="sub">Tickets you own as an MLOps Engineer. Click a card to open that list. Updates automatically.</p>
    <div className="grid">
      <Stat label="Total Assigned to Me" value={o.TOTAL} icon="list" color="#4f46e5" bg="#eef2ff" onClick={()=>go('assigned','ALL')}/>
      <Stat label="To Start" value={o.ASSIGNED} icon="user" color={SC.ASSIGNED[0]} bg={SC.ASSIGNED[1]} onClick={()=>go('assigned','ASSIGNED')} sub="Assigned, not started"/>
      <Stat label="In Progress" value={o['IN PROGRESS']} icon="clock" color={SC['IN PROGRESS'][0]} bg={SC['IN PROGRESS'][1]} onClick={()=>go('assigned','IN PROGRESS')}/>
      <Stat label="On Hold" value={o['ON HOLD']} icon="pause" color={SC['ON HOLD'][0]} bg={SC['ON HOLD'][1]} onClick={()=>go('assigned','ON HOLD')}/>
      <Stat label="Closed by Me" value={o.CLOSED} icon="check" color={SC.CLOSED[0]} bg={SC.CLOSED[1]} onClick={()=>go('assigned','CLOSED')} sub={`${cb.week} in the last 7 days`}/>
      <Stat label="Avg. Time to Close" value={fmtDur(cb.avg_s)} icon="clock" color="#7c3aed" bg="#f3e8ff" sub="From ticket creation"/>
      <Stat label="Replies Waiting" value={d.owned_need_reply} icon="bell" color="#dc2626" bg="#fef2f2" sub="Open tickets with a new message"/>
    </div>
    <div className="card"><CardHead title={`My Active Tickets (${rows.length})`}><button className="btn ghost" onClick={()=>go('assigned','ALL')}>View all</button></CardHead>
      <ReqTable rows={rows} go={go} requester empty="Nothing to work on right now."/></div>
    <div className="card"><CardHead title="Requests I Raised"><button className="btn ghost" onClick={()=>go('my','ALL')}>View</button></CardHead><StatusBar c={d.raised}/></div></div>;
}
function AdminDash({me,go}){
  const [d,setD]=useState(null);
  useLive(()=>api('/dashboard').then(setD));
  if(!d)return <div className="card">Loading…</div>;
  const a=d.all,k=d.kpi,pmax=Math.max(1,...PRIOS.map(p=>d.priority_open[p]));
  return <div><h2 className="hello">Admin Dashboard</h2><p className="sub">Every ticket in the service desk. Click a card to open that list. Updates automatically.</p>
    <StatusCards c={a} pick={s=>go('queue',s)} total="All Tickets"/>
    <div className="grid">
      <Stat label="Needs an Owner" value={a.NEW} icon="bell" color="#dc2626" bg="#fef2f2" onClick={()=>go('queue','NEW')} sub="Unassigned new tickets"/>
      <Stat label="Open High / Critical" value={k.urgent_open} icon="clock" color="#c2410c" bg="#fff7ed" sub="Not closed yet"/>
      <Stat label="Created (7 days)" value={k.created_7d} icon="plus" color="#4f46e5" bg="#eef2ff"/>
      <Stat label="Closed (7 days)" value={k.closed_7d} icon="check" color="#16a34a" bg="#f0fdf4"/>
      <Stat label="Avg. Time to Close" value={fmtDur(k.avg_resolution_s)} icon="clock" color="#7c3aed" bg="#f3e8ff" sub="All closed tickets"/>
      <Stat label="My Owned (open)" value={d.owned.OPEN} icon="user" color="#0369a1" bg="#e0f2fe" onClick={()=>go('assigned','ALL')} sub="Tickets you took ownership of"/>
    </div>
    <div className="cols">
      <div className="card"><CardHead title="Status Breakdown"/><StatusBar c={a}/>
        <h4 style={{margin:'22px 0 10px'}}>Open Tickets by Priority</h4>
        {PRIOS.map(p=><div key={p} className="hbar"><span>{p}</span><div><i style={{width:(d.priority_open[p]/pmax*100)+'%',background:PC[p]}}/></div><b>{d.priority_open[p]}</b></div>)}</div>
      <div className="card"><CardHead title="Users"><button className="btn ghost" onClick={()=>go('users')}>Manage</button></CardHead>
        <table className="kv"><tbody>{ROLES.map(r=><Row key={r} k={ROLE_NAME[r]+'s'}>{d.users[r]}</Row>)}<Row k="Invited (not signed up)">{d.users.invited}</Row></tbody></table></div>
    </div>
    <div className="card"><CardHead title="Team Workload"><span className="sub" style={{margin:0}}>Open tickets each person owns, and tickets they have closed</span></CardHead>
      <div className="tw"><table><thead><tr><th>Name</th><th>Role</th><th>Assigned</th><th>In Progress</th><th>On Hold</th><th>Open Total</th><th>Closed</th></tr></thead><tbody>
      {d.workload.map(w=><tr key={w.id}><td>{w.name}<div className="ssub">{w.email}{w.active?'':' · invited'}</div></td><td><Badge v={w.role} t={ROLE_NAME[w.role]}/></td>
        <td>{w.assigned}</td><td>{w.in_progress}</td><td>{w.on_hold}</td><td><b>{w.assigned+w.in_progress+w.on_hold}</b></td><td>{w.closed}</td></tr>)}
      {!d.workload.length&&<tr><td colSpan="7" className="empty">No MLOps Engineers yet. Assign a ticket to someone's email to add one.</td></tr>}</tbody></table></div></div>
    <div className="card"><CardHead title="Recent Activity"/>
      {d.recent.map(h=><div key={h.id} className="act"><div><b>{h.actor||'System'}</b> {(ACTION[h.action]||h.action).toLowerCase()} <a className="l" onClick={()=>go('detail',h.rid)}>{h.rid}</a>
        {h.to_status&&h.from_status!==h.to_status?<> → <Badge v={h.to_status}/></>:null}<div className="ssub">{h.title}</div></div><small>{h.created_at}</small></div>)}
      {!d.recent.length&&<div className="empty">No activity yet.</div>}</div></div>;
}
function Raise({go,flash}){
  const [f,setF]=useState({title:'',description:'',type:TYPES[0],priority:'HIGH',env:'Production',repo:'',branch:'',pr_url:''}),[busy,setBusy]=useState(false);
  const [imgs,setImgs]=useState([]),[ac,setAc]=useState({type:'Git Repository',resource:'',level:'Read only',for:'',until:''});
  const u=k=>e=>setF({...f,[k]:e.target.value}),ua=k=>e=>setAc({...ac,[k]:e.target.value});
  const isAcc=f.type==='Access Request',isOther=f.type==='Other',shots=isOther,[resLabel,resHint]=ACCESS_TYPES[ac.type];
  const submit=async e=>{e.preventDefault();setBusy(true);try{const r=await api('/requests','POST',{...f,images:shots?imgs:[],access:isAcc?ac:{}});chime();go('submitted',r.rid)}catch(x){flash(x.message);setBusy(false)}};
  return <div className="card" style={{maxWidth:820}}><h2>Raise Request</h2><p className="sub">Fill in the details. The admin will assign an owner.</p><form onSubmit={submit}>
    <F l="Request Title" value={f.title} onChange={u('title')} required maxLength="200" placeholder={isAcc?'Access to the ml-model-service repository':'Deploy model-service'}/>
    <label>{isAcc?'Why do you need this access?':'Description'}<textarea rows="4" value={f.description} onChange={u('description')} required
      placeholder={isAcc?'I need to push fixes to the model-service repository.':'Please deploy the model-service to production.'}/></label>
    <div className="row2"><label>Request Type<select value={f.type} onChange={u('type')}>{TYPES.map(t=><option key={t}>{t}</option>)}</select></label>
    <label>Priority<select value={f.priority} onChange={u('priority')}>{PRIOS.map(t=><option key={t}>{t}</option>)}</select></label>
    {isAcc&&<label>Access To<select value={ac.type} onChange={ua('type')}>{Object.keys(ACCESS_TYPES).map(t=><option key={t}>{t}</option>)}</select></label>}
    <label>Environment<select value={f.env} onChange={u('env')}>{ENVS.map(t=><option key={t}>{t}</option>)}</select></label>
    {!isAcc&&!isOther&&<F l="Repository" required value={f.repo} onChange={u('repo')} placeholder="ml-model-service"/>}</div>
    {isAcc?<>
      {ac.type!=='Model Endpoint'&&<F l={resLabel} required value={ac.resource} onChange={ua('resource')} placeholder={resHint} maxLength="300"/>}
      {ac.type==='Git Repository'&&<label>Access Level<select value={ac.level} onChange={ua('level')}>{ACCESS_LEVELS.map(t=><option key={t}>{t}</option>)}</select></label>}</>
    :!isOther&&<>
      {f.type==='Deployment Request'&&<F l="Branch Name" required value={f.branch} onChange={u('branch')} placeholder="release/v2.4" maxLength="200"
        pattern="[\w][\w.\/\-]*" title="Letters, numbers and _ . / - only (no spaces)"/>}
      <F l="Pull Request URL (optional)" type="url" value={f.pr_url} onChange={u('pr_url')} placeholder="https://github.com/.../pull/245"/></>}
    {shots&&<ImgPicker imgs={imgs} set={setImgs} flash={flash}/>}
    <button className="btn" disabled={busy}>Submit Request</button></form></div>;
}
function Submitted({rid,go}){
  return <div className="card" style={{maxWidth:520,margin:'30px auto',textAlign:'center'}}><div className="ok"><Ic n="check" s={44}/></div>
    <h2>Request Submitted Successfully!</h2><p>Request ID <b>{rid}</b><br/><br/>Status <Badge v="NEW"/></p><p style={{color:'var(--mu)'}}>The admin has been notified and will assign an owner.</p>
    <button className="btn" onClick={()=>go('detail',rid)}>View Request</button></div>;
}
function TicketList({src,go,initial,requester}){
  const [s,setS]=useState(STAT.includes(initial)?initial:'ALL'),[d,setD]=useState({rows:[],counts:{}});
  useLive(()=>api(`${src}?s=${encodeURIComponent(s)}`).then(setD),[src,s]);
  return <div className="card"><Tabs v={s} set={setS} items={[['ALL',`All (${d.counts.TOTAL||0})`],...STAT.map(k=>[k,`${LABEL[k]} (${d.counts[k]||0})`])]}/>
    <ReqTable rows={d.rows} go={go} requester={requester} empty={s==='ALL'?'No tickets yet.':`No ${LABEL[s].toLowerCase()} tickets.`}/></div>;
}
function Users({me,flash}){
  const [rows,setRows]=useState([]);
  const load=()=>api('/users').then(setRows).catch(e=>flash(e.message));
  useEffect(()=>{load()},[]);
  const setRole=async(u,role)=>{try{await api(`/users/${u.id}/role`,'POST',{role});flash(`${u.email} is now ${ROLE_NAME[role]}`)}catch(e){flash(e.message)}load()};
  const del=async u=>{if(!confirm(`Delete ${u.name} (${u.email})?\n\nThey will no longer be able to sign in. Tickets they raised or worked on are kept. This cannot be undone.`))return;
    try{const d=await api(`/users/${u.id}`,'DELETE');flash(d.how==='deleted'?`${u.email} was deleted.`:`${u.email} was deleted. Their name stays on the tickets they worked on.`)}catch(e){flash(e.message)}load()};
  return <div className="card"><p className="sub">Users raise tickets. MLOps Engineers work on tickets assigned to them. Admins assign tickets, take ownership and manage roles.
    Assigning a ticket to someone makes them an MLOps Engineer automatically.</p>
    <div className="tw"><table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Account</th><th>Open Tickets Owned</th><th>Joined</th><th></th></tr></thead><tbody>
    {rows.map(u=><tr key={u.id}><td>{u.name}</td><td>{u.email}</td>
      <td>{u.id===me.id?<Badge v={u.role} t={ROLE_NAME[u.role]}/>:<select value={u.role} onChange={e=>setRole(u,e.target.value)} style={{margin:0,width:'auto',padding:'6px 10px'}}>{ROLES.map(r=><option key={r} value={r}>{ROLE_NAME[r]}</option>)}</select>}</td>
      <td>{u.active?'Active':'Invited (not signed up yet)'}</td><td>{u.owned_open}</td><td>{u.created_at}</td>
      <td>{u.id!==me.id&&<button className="btn ghost sm del" onClick={()=>del(u)} title={`Delete ${u.email}`}>Delete</button>}</td></tr>)}
    {!rows.length&&<tr><td colSpan="7" className="empty">No users.</td></tr>}</tbody></table></div></div>;
}
// Theme: 'light' | 'dark' | 'system' (follows the computer). Stored per browser; applied as data-theme on <html>.
const getTheme=()=>{try{return localStorage.getItem('theme')||'light'}catch{return 'light'}};
const applyTheme=t=>{document.documentElement.dataset.theme=(t==='dark'||(t==='system'&&matchMedia('(prefers-color-scheme: dark)').matches))?'dark':'light'};
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{if(getTheme()==='system')applyTheme('system')});
function Settings({flash}){
  const [theme,setTheme]=useState(getTheme());
  const pick=t=>{setTheme(t);try{localStorage.setItem('theme',t)}catch{}applyTheme(t)};
  const [cur,setCur]=useState(''),[p,setP]=useState(''),[c,setC]=useState(''),[busy,setBusy]=useState(false);
  const submit=async e=>{e.preventDefault();if(p!==c)return flash('New passwords do not match');setBusy(true);
    try{await api('/auth/change-password','POST',{current:cur,password:p});setCur('');setP('');setC('');flash('Password changed.')}catch(x){flash(x.message)}setBusy(false)};
  return <div style={{maxWidth:560}}>
    <div className="card"><h3>Appearance</h3><p className="sub" style={{marginBottom:14}}>Choose how the Service Desk looks on this browser.</p>
      <div className="seg3" role="group" aria-label="Theme">{[['light','sun','Light'],['dark','moon','Dark'],['system','monitor','System']].map(([k,ic,l])=>
        <button key={k} type="button" className={theme===k?'on':''} aria-pressed={theme===k} onClick={()=>pick(k)}><Ic n={ic} s={16}/>{l}</button>)}</div>
      {theme==='system'&&<p className="ssub" style={{marginTop:10}}>Follows your computer's light or dark setting.</p>}</div>
    <div className="card"><h3>Change Password</h3><p className="sub" style={{marginBottom:14}}>Use at least 8 characters. You stay logged in on this device.</p><form onSubmit={submit}>
      <PwField l="Current Password" value={cur} onChange={e=>setCur(e.target.value)} required autoComplete="current-password"/>
      <PwField l="New Password" value={p} onChange={e=>setP(e.target.value)} required minLength="8" autoComplete="new-password"/>
      <PwField l="Confirm New Password" value={c} onChange={e=>setC(e.target.value)} required autoComplete="new-password"/>
      <button className="btn" disabled={busy}>Change Password</button></form></div>
  </div>;
}
function Notifications({go,refresh}){
  const [rows,setRows]=useState([]);
  useEffect(()=>{api('/notifications').then(setRows);api('/notifications/read','POST').then(refresh)},[]);
  return <div className="card">{rows.map(n=><div className="nt" key={n.id}><div className="ic"><Ic n="bell" s={18}/></div><div>
    {n.rid&&<b><a className="l" onClick={()=>go('detail',n.rid)}>{n.rid}</a></b>} <small style={{color:'var(--mu)'}}>{n.created_at}</small>
    {n.message.split('\n').map((l,i)=><div key={i}>{l}</div>)}</div></div>)}{!rows.length&&<div className="empty">No notifications.</div>}</div>;
}

/* ---------- Ticket workflow ---------- */
function PersonPicker({value,onChange}){
  const [team,setTeam]=useState([]),[other,setOther]=useState(false);
  useEffect(()=>{api('/team').then(setTeam).catch(()=>{})},[]);
  return <>
    <label>Team member<select required value={other?'*':value} onChange={e=>{const v=e.target.value;setOther(v==='*');onChange(v==='*'?'':v)}}>
      <option value="" disabled>Select a person</option>
      {team.map(p=><option key={p.email} value={p.email}>{p.name} ({p.email})</option>)}
      <option value="*">Someone else (type their email)</option></select></label>
    {other&&<F l="Email" type="email" required value={value} onChange={e=>onChange(e.target.value)} placeholder="name@iitmpravartak.net" autoComplete="off"/>}</>;
}
function AdminPanel({r,act}){
  const [email,setEmail]=useState(''),[cc,setCc]=useState(r.cc||''),[busy,setBusy]=useState(false);
  const run=async(p,b,ok)=>{setBusy(true);await act(p,b,ok);setBusy(false)};
  const submit=e=>{e.preventDefault();const to=email.trim();
    run('/assign',{email:to,cc},d=>d.email_error?`Assigned to ${to}, but the email was NOT sent: ${d.email_error}`:`Assigned to ${to}. Email sent.`)};
  return <div><h3>Admin Actions</h3>
    {!r.is_owner&&<div className="ownbox"><div><b>Take Ownership</b><div style={{color:'var(--mu)'}}>Become responsible for this ticket and resolve and close it yourself.</div></div>
      <button className="btn" disabled={busy} onClick={()=>run('/take-ownership',undefined,`You now own ${r.rid}. Use Start Work when you begin.`)}><Ic n="user" s={16}/> Take Ownership</button></div>}
    <form onSubmit={submit} style={{maxWidth:560,marginTop:18}}><h4 style={{margin:'0 0 8px'}}>{r.assignee_id?'Reassign to a team member':'Assign to a team member'}</h4>
      <PersonPicker value={email} onChange={setEmail}/>
      <F l="Also notify (optional, comma-separated emails)" value={cc} onChange={e=>setCc(e.target.value)} placeholder="lead@iitmpravartak.net, qa@iitmpravartak.net"/>
      <button className="btn" disabled={busy||!email.trim()}><Ic n="send" s={16}/> Assign &amp; Send Email</button></form></div>;
}
// Pop-up for "Failed / Bug Fix Required": describe the bugs, attach screenshots, send to the requester's team
function FailModal({act,flash,close}){
  const [desc,setDesc]=useState(''),[imgs,setImgs]=useState([]),[busy,setBusy]=useState(false);
  const submit=async e=>{e.preventDefault();setBusy(true);
    if(await act('/fail',{description:desc,images:imgs},'Marked as Failed / Bug Fix Required. The requester has been notified.'))close(true);else setBusy(false)};
  return <div className="modal" onClick={e=>{if(e.target===e.currentTarget)close()}}><form onSubmit={submit}>
    <h3 style={{marginTop:0}}>Failed / Bug Fix Required</h3><p className="sub">Describe what went wrong. The requester and the team are notified so they can fix it and resubmit.</p>
    <label>Bugs or errors encountered *<textarea rows="5" required autoFocus value={desc} onChange={e=>setDesc(e.target.value)}
      placeholder={'Pod crashes on start-up: ModuleNotFoundError: sklearn\n/predict returns 500 for empty input'}/></label>
    <ImgPicker imgs={imgs} set={setImgs} flash={flash}/>
    <div className="actions"><button className="btn" style={{background:'#dc2626'}} disabled={busy}><Ic n="send" s={16}/> Submit Bug Report</button>
      <button type="button" className="btn ghost" onClick={()=>close()}>Cancel</button></div></form></div>;
}
function CloseForm({env,acc,act,cancel,flash}){
  const [fail,setFail]=useState(false);
  const [c,setC]=useState({deploy_status:(acc?ACCESS_STAT:DEPLOY)[0],deployed_at:nowLocal(),notes:'',issues:'',resolution:'',deploy_url:'',version:'',env,vpn_user:'',vpn_password:'',api_key:'',files:[]});
  const [busy,setBusy]=useState(false);
  const u=k=>e=>setC({...c,[k]:e.target.value});
  const vpn=acc&&acc.type==='VPN'&&c.deploy_status!=='Rejected',ep=acc&&acc.type==='Model Endpoint'&&c.deploy_status!=='Rejected';
  const addPdfs=async e=>{const files=[...e.target.files];e.target.value='';
    if(c.files.length+files.length>5)return alert('You can attach up to 5 PDF files');
    const big=files.find(x=>x.size>10*1024*1024);if(big)return alert(`${big.name} is larger than 10 MB`);
    const bad=files.find(x=>!/\.pdf$/i.test(x.name));if(bad)return alert(`${bad.name} is not a PDF file`);
    const add=await readFiles(files);setC(c=>({...c,files:[...c.files,...add]}))};
  const submit=async e=>{e.preventDefault();setBusy(true);await act('/close',acc?{...c,deployed_at:nowLocal(),files:vpn?c.files:[],deploy_url:ep?c.deploy_url:'',api_key:'',notes:ep?'Model endpoint URL shared':c.notes}:c,'Ticket closed. Everyone involved has been notified.');setBusy(false)};
  if(acc)return <form onSubmit={submit} style={{marginTop:16}}><h3>Close Access Request</h3><p className="sub">Fields marked * are required.</p>
    <label>Result *<select value={c.deploy_status} onChange={u('deploy_status')}>{ACCESS_STAT.map(t=><option key={t}>{t}</option>)}</select></label>
    {ep?<F l="Model Endpoint URL *" type="url" required value={c.deploy_url} onChange={u('deploy_url')} placeholder="https://ml.example.com/v1/fraud-model"/>
    :<label>{c.deploy_status==='Rejected'?'Reason for rejecting *':'Access Given *'}<textarea rows="3" required value={c.notes} onChange={u('notes')}
      placeholder={c.deploy_status==='Rejected'?'Why the request was rejected':acc.type==='VPN'?'VPN account created on the office VPN':'Added to the ml-devs group with read-write on the repository'}/></label>}
    {vpn&&<><div className="row2"><F l="VPN Username *" required autoComplete="off" value={c.vpn_user} onChange={u('vpn_user')} placeholder="janani.n"/>
      <F l="VPN Password *" required autoComplete="new-password" value={c.vpn_password} onChange={u('vpn_password')} placeholder="Temporary password"/></div>
    <label>Attach PDFs (optional, up to 5 files, 10 MB each), e.g. VPN config or setup guide
      <input type="file" accept="application/pdf,.pdf" multiple onChange={addPdfs}/></label>
    {c.files.length>0&&<ul className="pdfs">{c.files.map((x,i)=><li key={i}><Ic n="file" s={15}/>{x.name}
      <button type="button" className="btn ghost sm" onClick={()=>setC({...c,files:c.files.filter((_,j)=>j!==i)})}>Remove</button></li>)}</ul>}</>}
    <div className="actions"><button className="btn g" disabled={busy}><Ic n="check" s={16}/> Close Ticket</button><button type="button" className="btn ghost" onClick={cancel}>Cancel</button></div></form>;
  return <><form onSubmit={submit} style={{marginTop:16}}><h3>Deployment Result</h3><p className="sub">Fields marked * are required.</p>
    <div className="row2"><label>Deployment Status *<select value={DEPLOY[0]} onChange={e=>{if(e.target.value===FAILED)setFail(true)}}>{DEPLOY.map(t=><option key={t}>{t}</option>)}</select></label>
    <F l="Deployment Date / Time *" type="datetime-local" required value={c.deployed_at} onChange={u('deployed_at')}/></div>
    <F l="Deployment / Application URL *" type="url" required value={c.deploy_url} onChange={u('deploy_url')} placeholder="https://ml-service.example.com"/>
    <div className="row2"><F l="Version" value={c.version} onChange={u('version')} placeholder="v2.4.1"/>
    <label>Environment<select value={c.env} onChange={u('env')}>{ENVS.map(t=><option key={t}>{t}</option>)}</select></label></div>
    <label>Deployment Notes (optional)<textarea rows="2" value={c.notes} onChange={u('notes')} placeholder="What was deployed and how"/></label>
    <div className="actions"><button className="btn g" disabled={busy}><Ic n="check" s={16}/> Mark as Deployed &amp; Close</button><button type="button" className="btn ghost" onClick={cancel}>Cancel</button></div></form>
    {fail&&<FailModal act={act} flash={flash} close={done=>{setFail(false);if(done)cancel()}}/>}</>;
}
function OwnerPanel({r,act,flash}){
  const [mode,setMode]=useState(null),[reason,setReason]=useState('');
  const hold=async e=>{e.preventDefault();if(await act('/hold',{reason},'Ticket put ON HOLD')){setMode(null);setReason('')}};
  if(r.fix_open)return <div className="section"><h3>Work on this Ticket</h3><div className="note warn" style={{marginTop:0}}>This deployment is waiting for a bug fix.
    Once {r.rname} (or you) fixes it and uses <b>Resubmit for Redeployment</b>, it comes back to you to redeploy.</div></div>;
  return <div className="section"><h3>Work on this Ticket</h3><p className="sub" style={{marginBottom:14}}>You own this ticket. Only you can update its status and close it.</p>
    <div className="actions">
      {r.status==='ASSIGNED'&&<button className="btn" onClick={()=>act('/start',undefined,'Ticket moved to IN PROGRESS')}><Ic n="clock" s={16}/> Start Work</button>}
      {r.status==='IN PROGRESS'&&mode!=='hold'&&<button className="btn ghost" onClick={()=>setMode('hold')}>Put On Hold</button>}
      {r.status==='ON HOLD'&&<button className="btn" onClick={()=>act('/resume',undefined,'Work resumed')}>Resume Work</button>}
      {mode!=='close'&&<button className="btn g" onClick={()=>setMode('close')}><Ic n="check" s={16}/> {r.access?'Close Ticket':'Record Deployment Result'}</button>}</div>
    {mode==='hold'&&<form onSubmit={hold} style={{marginTop:16,maxWidth:560}}>
      <label>Reason for hold *<textarea rows="2" required value={reason} onChange={e=>setReason(e.target.value)} placeholder="Waiting for credentials from the requester"/></label>
      <div className="actions"><button className="btn">Put On Hold</button><button type="button" className="btn ghost" onClick={()=>setMode(null)}>Cancel</button></div></form>}
    {mode==='close'&&<CloseForm env={r.env} acc={r.access} act={act} flash={flash} cancel={()=>setMode(null)}/>}</div>;
}
function Secret({v,show}){
  const [see,setSee]=useState(!!show),[ok,setOk]=useState(false);
  const copy=()=>navigator.clipboard.writeText(v).then(()=>{setOk(true);setTimeout(()=>setOk(false),1500)});
  return <span className="secret"><code>{see?v:'••••••••'}</code>{!show&&<button type="button" className="btn ghost sm" onClick={()=>setSee(!see)}>{see?'Hide':'Show'}</button>}
    <button type="button" className="btn ghost sm" onClick={copy}>{ok?'Copied':'Copy'}</button></span>;
}
function Pdfs({rid,files}){
  const open=async x=>{try{const d=await api(`/requests/${rid}/files/${x.id}`);const b=await (await fetch(d.src)).blob();
    const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=d.name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),5000)}catch(e){alert(e.message)}};
  return <ul className="pdfs">{files.map(x=><li key={x.id}><Ic n="file" s={15}/>{x.name}<button type="button" className="btn ghost sm" onClick={()=>open(x)}>Download</button></li>)}</ul>;
}
function BugReports({r}){
  return <div className="section"><h3>Deployment Bugs &amp; Fixes ({r.bugs.length})</h3>{r.bugs.map((x,i)=><div key={x.id} className={'bugrep'+(x.fixed_at?' fixed':'')}>
    <div style={{display:'flex',gap:10,alignItems:'center',flexWrap:'wrap',marginBottom:6}}><b>Attempt #{i+1} failed</b>
      {x.fixed_at?<Badge v="CLOSED" t="FIXED & RESUBMITTED"/>:<Badge v="BUG FIX" t="BUG FIX REQUIRED"/>}</div>
    <table className="kv"><tbody><Row k="Reported By">{x.reported_by_name} · {x.reported_at}</Row><Row k="Bugs / Errors">{x.description}</Row>
      {x.images.length>0&&<Row k="Screenshots"><Shots rid={r.rid} images={x.images}/></Row>}
      {x.fixed_at&&<><Row k="Fixed By">{x.fixed_by_name} · {x.fixed_at}</Row><Row k="What Was Fixed">{x.fix_notes}</Row></>}</tbody></table></div>)}</div>;
}
function Resubmit({r,act}){
  const [f,setF]=useState({fix_notes:'',branch:r.branch||'',pr_url:r.pr_url||''}),[busy,setBusy]=useState(false);
  const u=k=>e=>setF({...f,[k]:e.target.value});
  const submit=async e=>{e.preventDefault();setBusy(true);await act('/resubmit',f,'Resubmitted. The owner has been asked to redeploy.');setBusy(false)};
  return <form className="section" onSubmit={submit} style={{maxWidth:720}}><h3>Fix the Bugs and Resubmit</h3>
    <p className="sub">Review the bugs and screenshots above, fix them, then resubmit. The deployment goes back to {r.aname||'the owner'} to redeploy.</p>
    <label>What was fixed? *<textarea rows="3" required value={f.fix_notes} onChange={u('fix_notes')} placeholder="Added scikit-learn to requirements.txt and validated empty input"/></label>
    {r.type==='Deployment Request'&&<div className="row2"><F l="Branch to Deploy" value={f.branch} onChange={u('branch')} placeholder="hotfix/v2.4.2" maxLength="200"
      pattern="[\w][\w.\/\-]*" title="Letters, numbers and _ . / - only (no spaces)"/>
      <F l="Pull Request URL" type="url" value={f.pr_url} onChange={u('pr_url')} placeholder="https://github.com/.../pull/246"/></div>}
    <button className="btn g" disabled={busy}><Ic n="send" s={16}/> Resubmit for Redeployment</button></form>;
}
function Closure({r}){
  if(r.access)return <div className="done-box"><b>Closure Details</b><table className="kv"><tbody>
    <Row k="Closed By">{r.closed_by_name?`${r.closed_by_name} <${r.closed_by_email}>`:'-'}</Row><Row k="Closed At">{r.closed_at||'-'}</Row>
    <Row k="Result"><b>{r.deploy_status||'-'}</b></Row>{r.access.type==='Model Endpoint'&&r.deploy_status!=='Rejected'?null:<Row k={r.deploy_status==='Rejected'?'Reason':'Access Given'}>{r.deploy_notes||'-'}</Row>}
    {r.resolution?<Row k="Resolution / Instructions">{r.resolution}</Row>:null}{r.deploy_url?<Row k={r.access.type==='Model Endpoint'?'Model Endpoint URL':'Link'}><Url u={r.deploy_url}/></Row>:null}
    {r.creds?.vpn_user&&<><Row k="VPN Username"><Secret v={r.creds.vpn_user} show/></Row><Row k="VPN Password"><Secret v={r.creds.vpn_password}/></Row></>}
    {r.creds?.api_key&&<Row k="API Key / Token"><Secret v={r.creds.api_key}/></Row>}
    {r.files.length>0&&<Row k="Attached PDFs"><Pdfs rid={r.rid} files={r.files}/></Row>}</tbody></table></div>;
  return <div className="done-box"><b>Closure Details</b><table className="kv"><tbody>
    <Row k="Closed By">{r.closed_by_name?`${r.closed_by_name} <${r.closed_by_email}>`:'-'}</Row><Row k="Closed At">{r.closed_at||'-'}</Row>
    <Row k="Deployment Status"><b>{r.deploy_status||'-'}</b></Row><Row k="Deployed At">{r.deployed_at||'-'}</Row><Row k="Deployment URL"><Url u={r.deploy_url}/></Row>
    <Row k="Version">{r.version||'-'}</Row><Row k="Environment">{r.res_env||'-'}</Row>
    {r.deploy_notes?<Row k="Deployment Notes">{r.deploy_notes}</Row>:null}{r.issues&&r.issues!=='None'?<Row k="Issues Encountered">{r.issues}</Row>:null}
    {r.resolution?<Row k="Resolution">{r.resolution}</Row>:null}
  </tbody></table></div>;
}
function Shots({rid,images}){
  const [src,setSrc]=useState({}),[big,setBig]=useState(null);
  useEffect(()=>{images.forEach(x=>api(`/requests/${rid}/images/${x.id}`).then(d=>setSrc(s=>({...s,[x.id]:d.src}))).catch(()=>{}))},[rid,images.length]);
  return <><div className="shots">{images.map(x=><div key={x.id} className="shot">{src[x.id]?<img src={src[x.id]} alt={x.name} title={x.name} onClick={()=>setBig(src[x.id])}/>:<span>Loading…</span>}</div>)}</div>
    {big&&<div className="lightbox" onClick={()=>setBig(null)}><img src={big} alt=""/></div>}</>;
}
const dur=s=>{s=Math.max(0,Math.round(s));const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return d?`${d}d ${h}h`:h?`${h}h ${m}m`:m?`${m}m${s%60?` ${s%60}s`:''}`:`${s}s`};
// Text for the line between TRACK[i] and TRACK[i+1]. A skipped step (e.g. closed without Start Work) has no time,
// so the next measured line starts from the last step that did happen.
function segText(r,i,cur){
  const t=r.tse||{},b=TRACK[i+1];let k=i;while(k>=0&&!t[TRACK[k]])k--;
  const from=k>=0?TRACK[k]:null,st=from&&t[from],name=s=>LABEL[s]||s;
  const hold=r.hold_secs>=1&&b==='CLOSED'?`includes ${dur(r.hold_secs)} on hold`:null;
  if(t[b]&&st)return [`${name(from)} → ${name(b)}: ${dur(t[b]-st)}`,from!==TRACK[i]?`${name(TRACK[i])} was skipped`:hold];
  if(!t[b]&&i+1<=cur)return [`${name(b)} was skipped`,b==='IN PROGRESS'?'Closed without pressing Start Work':null];
  if(i===cur&&st)return [`${name(from)} for ${dur(r.now-st)} so far`,r.status==='ON HOLD'?`On hold now (${dur(r.hold_secs)} on hold in total)`:r.hold_secs>=1?`includes ${dur(r.hold_secs)} on hold`:`Waiting for ${name(b)}`];
  return ['Not reached yet',null];
}
// Admins can mark someone else's deployment as failed without taking ownership
function AdminFail({act,flash}){
  const [open,setOpen]=useState(false);
  return <div style={{marginTop:18}}><button className="btn ghost" onClick={()=>setOpen(true)}><Ic n="send" s={16}/> Mark Failed / Bug Fix Required</button>
    {open&&<FailModal act={act} flash={flash} close={()=>setOpen(false)}/>}</div>;
}
function Detail({id,me,flash,refresh}){
  const [r,setR]=useState(null),[tab,setTab]=useState('details'),[body,setBody]=useState(''),[tip,setTip]=useState(null);
  const isA=me.role==='admin';
  const load=useCallback(()=>api('/requests/'+id).then(setR).catch(e=>flash(e.message)),[id]);
  useEffect(()=>{load()},[id]);
  // Follow changes made by other people (e.g. a failed deployment being resubmitted) without leaving the page
  useEffect(()=>{const t=setInterval(()=>{if(!document.hidden)api('/requests/'+id).then(setR).catch(()=>{})},10000);return()=>clearInterval(t)},[id]);
  if(!r)return <div className="card">Loading…</div>;
  const act=async(p,b,ok)=>{try{const d=await api('/requests/'+id+p,'POST',b);await load();refresh();if(ok)flash(typeof ok==='function'?ok(d):ok);return true}catch(e){flash(e.message);await load();return false}};
  const active=ACTIVE.includes(r.status);
  const send=async e=>{e.preventDefault();if(await act('/comments',{body}))setBody('')};
  const cur=TRACK.indexOf(r.status==='ON HOLD'?'IN PROGRESS':r.status);
  return <div className="card"><div style={{display:'flex',gap:12,alignItems:'center',flexWrap:'wrap'}}><h2 style={{margin:0}}>{r.rid}</h2><StatusBadge r={r}/></div>
    <h3 style={{margin:'6px 0 16px',color:'var(--mu)',fontWeight:500}}>{r.title}</h3>
    <Tabs v={tab} set={setTab} items={[['details','Details'],['tracker','Tracker'],['conv','Conversation'],['history','Audit History']]}/>
    {tab==='details'&&<div><table className="kv"><tbody>
      <Row k="Requester">{`${r.rname} <${r.remail}>`}</Row><Row k="Type">{r.type}</Row><Row k="Priority"><Badge v={r.priority}/></Row><Row k="Environment">{r.env}</Row>
      {r.access?<><Row k="Access To">{r.access.type}</Row>{r.access.resource?<Row k={ACCESS_TYPES[r.access.type]?.[0]||'Server / System'}><b>{r.access.resource}</b></Row>:null}
        {r.access.level?<Row k="Access Level">{r.access.level}</Row>:null}{r.access.for!==r.remail?<Row k="Access For">{r.access.for.split(',').join(', ')}</Row>:null}{r.access.until?<Row k="Needed Until">{r.access.until}</Row>:null}</>
      :r.type!=='Other'&&<><Row k="Repository">{r.repo||'-'}</Row>{r.branch?<Row k="Branch"><b>{r.branch}</b></Row>:null}<Row k="Pull Request"><Url u={r.pr_url}/></Row></>}
      <Row k="Owner">{owner(r)}</Row><Row k="Ownership">{ownership(r)}{r.assigned_at?` on ${r.assigned_at}`:''}</Row>
      {r.cc?<Row k="Also Notified">{r.cc.split(',').join(', ')}</Row>:null}
      {r.status==='CLOSED'?<Row k="Closed By"><b>{`${r.closed_by_name} <${r.closed_by_email}>`}</b>{` on ${r.closed_at}`}</Row>:null}
      <Row k="Created">{r.created_at}</Row><Row k="Last Updated">{r.updated_at}</Row><Row k="Description">{r.description}</Row>
      {r.images.length>0&&<Row k="Screenshots"><Shots rid={r.rid} images={r.images}/></Row>}</tbody></table>
      {r.status==='CLOSED'&&<Closure r={r}/>}
      {r.bugs.length>0&&<BugReports r={r}/>}
      {r.fix_open&&(isA||r.is_owner||me.id===r.requester_id)&&<Resubmit key={r.bugs.length} r={r} act={act}/>}
      {r.is_owner&&active&&<OwnerPanel r={r} act={act} flash={flash}/>}
      {!r.is_owner&&active&&<div className="note"><b>{r.aname}</b> owns this ticket and is responsible for resolving and closing it.{isA?' Use Take Ownership below to work on it yourself.':''}</div>}
      {isA&&r.status!=='CLOSED'&&<div className="section"><AdminPanel key={r.assignee_id||0} r={r} act={act}/>
        {!r.access&&!r.fix_open&&!r.is_owner&&r.assignee_id&&<AdminFail act={act} flash={flash}/>}</div>}</div>}
    {tab==='tracker'&&<div><div className="track">{TRACK.map((s,i)=><div key={s} className={'step '+(i<=cur?'done ':'')+(i===cur?'cur':'')}><div className="dot">{i<=cur?<Ic n="check" s={16}/>:i+1}</div>
      <b>{i===cur&&r.status==='ON HOLD'?(r.fix_open?'BUG FIX REQUIRED':'ON HOLD'):s}</b>{i===cur&&r.status==='ON HOLD'?(r.fix_open?'Waiting for the fix':DESC['ON HOLD']):DESC[s]}<br/>{i<=cur?r.ts[s]||'':''}
        {i<TRACK.length-1&&(([a,b])=><span className={'seg'+(i<cur?' done':i===cur&&r.status!=='CLOSED'?' now':'')+(tip===i?' open':'')} tabIndex="0"
          onClick={()=>setTip(tip===i?null:i)} onBlur={()=>setTip(null)}><span className="tip">{a}{b&&<small>{b}</small>}</span></span>)(segText(r,i,cur))}</div>)}</div>
      {r.tse?.NEW&&<p><b>Total time:</b> {dur((r.tse.CLOSED||r.now)-r.tse.NEW)}{r.tse.CLOSED?' (New → Closed)':' so far'}{r.hold_secs>=1?`, including ${dur(r.hold_secs)} on hold`:''}
        <span style={{color:'var(--mu)'}}> · Hover or tap a line to see the time between steps</span></p>}
      <p><b>Current Status:</b> <Badge v={r.status}/> &nbsp; <b>Owner:</b> {owner(r)}</p>{r.status==='CLOSED'&&(r.access?<p><b>Result:</b> {r.deploy_status}</p>:<p><b>Deployment URL:</b> <Url u={r.deploy_url}/></p>)}</div>}
    {tab==='conv'&&<div><div className="chat">{r.comments.map((m,i)=><div key={i} className={'msg '+(m.author_id===me.id?'me':'')}><small><b>{m.name}</b> ({ROLE_NAME[m.role]}) · {m.created_at}</small>{m.body}</div>)}
      {!r.comments.length&&<div className="empty">No messages yet.</div>}</div>
      {r.status!=='CLOSED'&&<form className="chatbox" onSubmit={send}><textarea rows="2" required value={body} onChange={e=>setBody(e.target.value)} placeholder="Write a reply..."/><button className="btn"><Ic n="send" s={16}/> Send</button></form>}</div>}
    {tab==='history'&&<div><h3>Ticket Record</h3><table className="kv"><tbody>
      <Row k="Created By">{`${r.rname} <${r.remail}>`}</Row><Row k="Created At">{r.created_at}</Row>
      <Row k="Owner">{owner(r)}</Row><Row k="Ownership">{ownership(r)}</Row><Row k="Assigned / Taken At">{r.assigned_at||'-'}</Row>
      <Row k={r.access?'Result':'Deployment Status'}>{r.deploy_status||'-'}</Row><Row k={r.access?'Completed At':'Deployed At'}>{r.deployed_at||'-'}</Row><Row k="URL"><Url u={r.deploy_url}/></Row>
      <Row k="Closed By">{r.closed_by_name?`${r.closed_by_name} <${r.closed_by_email}>`:'-'}</Row><Row k="Closed At">{r.closed_at||'-'}</Row></tbody></table>
      <h3 style={{marginTop:24}}>Audit Trail</h3><div className="tw"><table><thead><tr><th>When</th><th>Action</th><th>Status</th><th>By</th><th>Details</th></tr></thead><tbody>
      {r.history.map(h=><tr key={h.id}><td style={{whiteSpace:'nowrap'}}>{h.created_at}</td><td>{ACTION[h.action]||h.action}</td>
        <td style={{whiteSpace:'nowrap'}}>{h.to_status?(h.from_status&&h.from_status!==h.to_status?`${h.from_status} → ${h.to_status}`:h.to_status):''}</td><td>{h.actor||'-'}</td><td>{h.detail}</td></tr>)}</tbody></table></div></div>}
  </div>;
}

/* ---------- Shell with LEFT sidebar ---------- */
function Shell({me,page,go,logout,children}){
  const [open,setOpen]=useState(false),[snd,setSnd]=useState(soundOn());
  const toggleSnd=()=>{const v=!snd;setSnd(v);try{localStorage.setItem('sound',v?'on':'off')}catch{}if(v)chime(true)};
  const isA=me.role==='admin';
  const nav=[['dashboard','dash','Dashboard'],
    ...(isA?[['queue','queue','All Tickets'],['assigned','user','My Owned Tickets'],['users','users','Users & Roles']]
    :me.role==='engineer'?[['assigned','user','Assigned to Me'],['raise','plus','Raise Request'],['my','list','My Requests']]
    :[['raise','plus','Raise Request'],['my','list','My Requests']]),
    ['notifs','bell','Notifications']];
  const home=isA?'queue':me.role==='engineer'?'assigned':'my';
  const active=(page.n==='detail'||page.n==='submitted')?home:page.n;
  const titles={dashboard:isA?'Admin Dashboard':'Dashboard',raise:'Raise Request',my:'My Requests',assigned:isA?'My Owned Tickets':'Assigned to Me',queue:'All Tickets',users:'Users & Roles',notifs:'Notifications',settings:'Settings',detail:'Ticket '+(page.a||''),submitted:'Request Submitted'};
  const ini=me.name.split(' ').map(x=>x[0]).join('').slice(0,2).toUpperCase();
  const nav2=k=>{setOpen(false);go(k)};
  return <div className="app"><div className={'shade '+(open?'on':'')} onClick={()=>setOpen(false)}/>
    <aside className={'side '+(open?'open':'')}>
      <div className="brand" role="link" tabIndex="0" title="Go to Dashboard" onClick={()=>nav2('dashboard')} onKeyDown={e=>{if(e.key==='Enter')nav2('dashboard')}}><img className="logo" src="/logo.png" alt="CHAI"/><div><b>MLOps</b><small>Service Desk</small></div></div>
      <div className="menu">{nav.map(([k,ic,l])=><div key={k} className={'nl '+(active===k?'on':'')} onClick={()=>nav2(k)}><Ic n={ic}/>{l}
        {k==='notifs'&&me.unread>0&&<span className="cnt">{me.unread}</span>}</div>)}</div>
      <div className="usr"><div className="av">{ini}</div><div className="i"><b>{me.name}</b><small>{ROLE_NAME[me.role]}</small></div></div>
      <div className={'nl set '+(active==='settings'?'on':'')} onClick={()=>nav2('settings')}><Ic n="gear"/>Settings</div>
      <div className="nl out" onClick={logout}><Ic n="out"/>Logout</div>
    </aside>
    <div className="main"><div className="top"><button className="burger" onClick={()=>setOpen(true)}><Ic n="menu"/></button><h1>{titles[page.n]||''}</h1>
      <button className="btn ghost" style={{marginLeft:'auto',padding:'8px 12px'}} onClick={toggleSnd} title={snd?'Mute notification sound':'Turn on notification sound'}><Ic n={snd?'vol':'mute'} s={18}/>{snd?'Sound on':'Sound off'}</button></div>
      <div className="content">{children}</div></div></div>;
}

// Email links point to /#/t/MLOPS-0001; open that ticket once the user is signed in
const hashRid=()=>(location.hash.match(/^#\/t\/([\w-]+)$/)||[])[1]||null;
let deepRid=hashRid();
function landing(u){
  if(deepRid){const a=deepRid;deepRid=null;history.replaceState(null,'',location.pathname);return {n:'detail',a}}
  return {n:'dashboard'};
}
function App(){
  const [me,setMe]=useState(null),[page,setPage]=useState({n:'login'}),[msg,setMsg]=useState(''),[ready,setReady]=useState(false),[reg,setReg]=useState({});
  const go=(n,a)=>{setMsg('');setPage({n,a})},flash=m=>setMsg(m);
  const seen=useRef(null),[stale,setStale]=useState(false),meRef=useRef(null);meRef.current=me;
  // A ticket link opened in a tab that already has the app loaded only changes the #hash, so watch for it
  useEffect(()=>{const h=()=>{const r=hashRid();if(!r)return;history.replaceState(null,'',location.pathname);
    if(meRef.current){setMsg('');setPage({n:'detail',a:r})}else deepRid=r};
    addEventListener('hashchange',h);return()=>removeEventListener('hashchange',h)},[]);
  const loadMe=async()=>{try{if(!localStorage.getItem('tok')&&!sessionStorage.getItem('tok'))throw 0;  // not signed in: skip the call
    const u=await api('/me');
    // First load only sets the baseline; afterwards a rising unread count means a new notification arrived
    if(seen.current!==null&&u.unread>seen.current){chime();if(u.latest)setMsg('🔔 '+u.latest)}
    seen.current=u.unread;setStale(u.build!=='__BUILD__');setMe(u);return u}catch{seen.current=null;setMe(null);return null}};
  const onLogin=async()=>{const u=await loadMe();if(u){setMsg('');setPage(landing(u))}};
  useEffect(()=>{(async()=>{const u=await loadMe();if(u)setPage(landing(u));setReady(true)})()},[]);
  useEffect(()=>{if(!me)return;const t=setInterval(loadMe,5000);return()=>clearInterval(t)},[!!me]);
  useEffect(()=>{if(!msg)return;const t=setTimeout(()=>setMsg(''),5000);return()=>clearTimeout(t)},[msg]);
  const logout=()=>{localStorage.removeItem('tok');sessionStorage.removeItem('tok');seen.current=null;setMe(null);go('login')};
  if(!ready)return <div style={{padding:40}}>Loading…</div>;
  const n=page.n,isA=me&&me.role==='admin';
  let body;
  if(!me){
    if(n==='register')body=<Register go={go} setReg={setReg} flash={flash}/>;
    else if(n==='forgot')body=<Register go={go} setReg={setReg} flash={flash} reset/>;
    else if(n==='verify')body=<Verify go={go} reg={reg} setReg={setReg} flash={flash}/>;
    else if(n==='password')body=<SetPassword go={go} reg={reg} flash={flash}/>;
    else body=<Login go={go} flash={flash} onLogin={onLogin}/>;
  }else{
    let c;
    const dash=isA?<AdminDash me={me} go={go}/>:me.role==='engineer'?<EngineerDash me={me} go={go}/>:<UserDash me={me} go={go}/>;
    if(n==='raise'&&!isA)c=<Raise go={go} flash={flash}/>;
    else if(n==='submitted')c=<Submitted rid={page.a} go={go}/>;
    else if(n==='my'&&!isA)c=<TicketList key={'my'+page.a} src="/requests" initial={page.a} go={go}/>;
    else if(n==='assigned'&&me.role!=='user')c=<TicketList key={'assigned'+page.a} src="/assigned" initial={page.a} go={go} requester/>;
    else if(n==='queue'&&isA)c=<TicketList key={'queue'+page.a} src="/queue" initial={page.a} go={go} requester/>;
    else if(n==='users'&&isA)c=<Users me={me} flash={flash}/>;
    else if(n==='notifs')c=<Notifications go={go} refresh={loadMe}/>;
    else if(n==='settings')c=<Settings flash={flash}/>;
    else if(n==='detail')c=<Detail key={page.a} id={page.a} me={me} flash={flash} refresh={loadMe}/>;
    else c=dash;
    body=<Shell me={me} page={page} go={go} logout={logout}>{c}</Shell>;
  }
  return <div>{stale&&<div className="update">A new version of the Service Desk is available. <button onClick={()=>location.reload()}>Reload</button></div>}
    {msg&&<div className="toast" onClick={()=>setMsg('')}>{msg}</div>}{body}</div>;
}
ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
</script></body></html>"""

# Changes whenever the page code changes; open tabs compare it with /api/me and offer a reload.
BUILD = hashlib.sha1(PAGE.encode()).hexdigest()[:10]
PAGE = PAGE.replace("__BUILD__", BUILD)

@app.get("/", response_class=HTMLResponse)
def index(): return HTMLResponse(PAGE, headers={"Cache-Control": "no-cache"})  # browsers pick up a new version after a restart

LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chai_logo.png")

@app.get("/logo.png", include_in_schema=False)
def logo(): return FileResponse(LOGO, media_type="image/png", headers={"Cache-Control": "no-cache"})

def import_sqlite(path):
    # One-time copy of user accounts (with their password hashes and roles) from the old SQLite database
    import sqlite3
    s = sqlite3.connect(path); s.row_factory = sqlite3.Row
    added = 0
    for x in s.execute("select name, email, pw, role from users"):
        r = run("""insert into users(name,email,pw_hash,role) values(%s,%s,%s,%s) on conflict (email) do nothing returning id""",
                (x["name"], x["email"].lower(), x["pw"], x["role"] if x["role"] in ROLES else "user"))
        added += bool(r)
    print(f"Imported {added} user(s) from {path}. Existing emails were left unchanged.")

if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--test-email":  # checks the SMTP settings without starting the server
        err = mail(args[1], "MLOps Service Desk test email", "If you can read this, email notifications are working.")
        print("\nTest email sent." if not err else f"\nTest email NOT sent: {err}"); sys.exit(1 if err else 0)
    if args == ["--ensure-admins"]:  # repair: apply ADMIN_EMAILS to the database now, then list the admins
        admins = start_db()
        print("\nAdmins now:", ", ".join(admins) or "NONE (set ADMIN_EMAILS in .env)"); pool.close(); sys.exit(0 if admins else 1)
    if len(args) == 2 and args[0] == "--import-sqlite":
        start_db(); import_sqlite(args[1]); pool.close(); sys.exit(0)
    import socket
    with socket.socket() as s:  # friendlier than uvicorn's bind error when a second copy is started
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            print(f"\nPort {PORT} is already in use, probably by another copy of this app that is still running.\n"
                  f"Open http://127.0.0.1:{PORT} to use it, or stop it first with:\n"
                  f"  Get-NetTCPConnection -LocalPort {PORT} -State Listen | ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force }}\n")
            sys.exit(1)
    # Uvicorn prints http://0.0.0.0:PORT, which browsers can't open; show the addresses people should use
    print(f"\n  Open on this PC:        http://127.0.0.:{PORT}" + (f"\n  Open from other PCs:    {APP_URL}" if HOST != "127.0.0.1" else "") + "\n", flush=True)
    uvicorn.run(app, host=HOST, port=PORT)
