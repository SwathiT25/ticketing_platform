# MLOps Service Desk

A ticketing platform for MLOps requests: deployments, access requests, bug fixes and general requests. Users raise tickets, an admin assigns each one to an MLOps Engineer (or takes ownership), and the owner works the ticket through to closure. Every step is emailed to the people involved and recorded in an audit trail.

| | |
|---|---|
| **Backend** | Python 3.12, FastAPI, served by Uvicorn |
| **Frontend** | React 18 single-page app, served by the backend (no build step) |
| **Database** | PostgreSQL 16 in Docker |
| **Email** | Any SMTP server (we use Microsoft 365 / Outlook) |
| **Packaging** | Docker: one `docker compose up -d --build` starts the app and the database |

The whole application (API, database access and web UI) is one file, `mlops_service_desk.py`. Tables are created automatically on startup.

**Quick start (Docker):**

```bash
git clone https://github.com/IITMPravartak-Chai/ticketing_platform.git
cd ticketing_platform
cp .env.example .env        # then fill in the real values
docker compose up -d --build
```

Then open `http://<server-ip>:8000`. The full steps are in [Deployment with Docker](#deployment-with-docker-recommended).

---

## Contents

1. [Project structure](#project-structure)
2. [How it fits together](#how-it-fits-together)
3. [Server requirements](#server-requirements)
4. [Deployment with Docker (recommended)](#deployment-with-docker-recommended)
5. [Deployment without Docker](#deployment-without-docker)
6. [Configuration reference (.env)](#configuration-reference-env)
7. [Admin accounts](#admin-accounts)
8. [Run it as a service (without Docker)](#run-it-as-a-service-without-docker)
9. [Updating, and rolling back](#updating-and-rolling-back)
10. [Backup and restore](#backup-and-restore)
11. [Logs](#logs)
12. [Security checklist](#security-checklist)
13. [Troubleshooting](#troubleshooting)
14. [Features](#features)
15. [Database tables](#database-tables)

---

## Project structure

```
ticketing_platform/
├── mlops_service_desk.py   # The application: REST API, database access, emails and the React UI (one file)
├── Dockerfile              # Builds the app image (Python 3.12, runs as a non-root user, health check)
├── .dockerignore           # Keeps .env, backups and dumps out of the image
├── docker-compose.yml      # Runs the app (servicedesk-app) and PostgreSQL 16 (servicedesk-postgres) together
├── requirements.txt        # Python packages, pinned versions
├── .env.example            # Template for .env: copy it and fill in real values
├── .env                    # Real settings and passwords. NOT in Git; created on each server
├── .gitignore              # Keeps .env, databases, dumps, backups and caches out of Git
└── README.md               # This file
```

Inside `mlops_service_desk.py`, the code runs top to bottom in this order:

| Section | What it does |
|---|---|
| Settings | Reads `.env`, defines ticket types, statuses, priorities and validation rules |
| Database | Connection pool and the `SCHEMA` (tables are created or upgraded on every start) |
| Auth | Sign-up with an emailed 6-digit code, login, forgot password, signed login tokens |
| Mail / notifications | Sends emails in the background and stores in-app notifications |
| Tickets | Raise, list, view, assign, take ownership, start, hold, resume, close, comment, screenshots |
| Users & roles | Admin role management and the team list for assigning |
| Frontend | The React page (`PAGE`), served at `/` |
| Command line | `--test-email` and `--import-sqlite` tools, then starts the web server |

---

## How it fits together

```
                          ┌──────────────── docker compose ─────────────────┐
 Users' browsers ──:8000──▶  servicedesk-app            servicedesk-postgres  │
        │                 │  mlops_service_desk.py ──▶ PostgreSQL 16          │
        │                 │  (FastAPI + Uvicorn)       volume: servicedesk_pgdata
        │                 └────────┼────────────────────────────────────────┘
        │                          └──SMTP :587──▶ smtp.office365.com  (notification emails)
        │
        └──HTTPS──▶ cdnjs.cloudflare.com  (React and Babel scripts loaded by the browser)
```

- The app serves both the web page (`/`) and the API (`/api/...`) on one port.
- Inside Docker the app reaches the database by its service name (`postgres:5432`).
- PostgreSQL is also published on `127.0.0.1:5434` for backups and admin tools, but only on the server itself, never on the network.
- Users' browsers need internet access to `cdnjs.cloudflare.com` to load React. Without it the page stays blank.

---

## Server requirements

| Need | Details |
|---|---|
| OS | Linux or Windows with Docker |
| Docker | Docker Engine with the Compose plugin (`docker compose`) |
| Python | Only for running without Docker: 3.12 or newer |
| Inbound port | `8000/tcp` (or the `APP_PORT` you choose) from the office network |
| Outbound | SMTP to `smtp.office365.com:587` |
| Hardware | 1 CPU, 1 GB RAM and a few GB of disk is plenty for a team |

---

## Deployment with Docker (recommended)

Docker runs the app and PostgreSQL as two containers. Nothing else needs to be installed on the server, and both containers restart on their own after a reboot.

### 1. Get the code

```bash
git clone https://github.com/IITMPravartak-Chai/ticketing_platform.git
cd ticketing_platform
git checkout Dev            # or the branch you are deploying
```

### 2. Create `.env`

```bash
cp .env.example .env        # Windows: Copy-Item .env.example .env
```

Fill in the values described in [step 2 below](#2-create-env-1). With Docker you do **not** need to set `DATABASE_URL`, `HOST` or `PORT`: `docker-compose.yml` sets them. To serve on a different port, set `APP_PORT`.

### 3. Build and start

```bash
docker compose up -d --build
docker compose ps           # both containers should show "healthy" after ~30 seconds
docker compose logs -f app  # live app log; Ctrl+C stops watching, the app keeps running
```

Every start creates or upgrades the tables and makes each address in `ADMIN_EMAILS` an admin (see [Admin accounts](#admin-accounts)). Check the log for `Created admin account` or `Promoted ... to admin`.

### 4. Check email, open the firewall, verify

```bash
docker compose exec app python mlops_service_desk.py --test-email you@yourcompany.com
```

Then open the firewall port and verify as in [steps 7 and 8 below](#7-open-the-firewall-port).

### Everyday Docker commands

| Task | Command |
|---|---|
| Start or update after a `git pull` | `docker compose up -d --build` |
| Stop (data is kept) | `docker compose down` |
| Restart the app only | `docker compose restart app` |
| App logs | `docker compose logs -f app` |
| Status and health | `docker compose ps` |
| SQL prompt | `docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB>` |

> ⚠️ Never run `docker compose down -v`. The `-v` deletes the database volume and all tickets with it.

### What the Docker setup does

- **Image:** `Dockerfile` builds from `python:3.12-slim`, installs the pinned `requirements.txt`, and runs the app as a non-root user. A health check calls the app every 30 seconds.
- **No secrets in the image:** `.env`, backups and dumps are excluded by `.dockerignore`, and settings are passed in at runtime from `.env`.
- **Time zone:** the container uses `TZ` (default `Asia/Kolkata`), so ticket times match local time.
- **Start order:** the app waits until PostgreSQL is healthy before starting.

---

## Deployment without Docker

Use this only if Docker can't run the app on your server. PostgreSQL still runs in Docker. Commands are shown for Linux, with Windows (PowerShell) equivalents where they differ.

### 1. Get the code

```bash
git clone https://github.com/IITMPravartak-Chai/ticketing_platform.git
cd ticketing_platform
```

### 2. Create `.env`

```bash
cp .env.example .env          # Windows: Copy-Item .env.example .env
```

Edit `.env` and set at least:

- `SECRET_KEY`: generate one with `python3 -c "import secrets; print(secrets.token_hex(32))"`.
- `POSTGRES_PASSWORD`, and the same password inside `DATABASE_URL` (not needed with Docker). Use letters and numbers only.
- `APP_URL`: the address users will open, for example `http://10.10.182.220:8000`. Links in emails use it.
- `ADMIN_EMAILS`: every address that must be an admin, comma-separated. `ADMIN_PASSWORD` (optional) is the starting password for admin accounts that don't exist yet. See [Admin accounts](#admin-accounts).
- `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`: the mailbox that sends notifications.
- `TEAM_EMAILS`: the people the admin assigns tickets to.

Every setting is described in the [configuration reference](#configuration-reference-env). The project owner can share the production values privately; they are never stored in Git.

### 3. Start PostgreSQL

```bash
docker compose up -d postgres  # only the database; the app runs with Python below
docker compose ps              # servicedesk-postgres should be "healthy"
```

The container restarts automatically with Docker (`restart: unless-stopped`). Data lives in the `servicedesk_pgdata` volume, so it survives container restarts and upgrades.

### 4. Install the Python packages

```bash
python3 -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 5. Check that email works

```bash
python mlops_service_desk.py --test-email you@yourcompany.com
```

It prints `Test email sent.` on success, or the SMTP error.

### 6. Start the app

```bash
python mlops_service_desk.py
```

Every start creates or upgrades the tables and applies `ADMIN_EMAILS`. You should see the links to open, then `Uvicorn running on ...`.

### 7. Open the firewall port

```bash
sudo ufw allow 8000/tcp                                    # Linux with ufw
```
```powershell
# Windows (run as Administrator)
New-NetFirewallRule -DisplayName "MLOps Service Desk" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

### 8. Verify

1. Open `APP_URL` from another machine. The login page should appear.
2. Log in with the admin account. **Change its password straight away** with *Forgot password?*.
3. Raise a test ticket as a normal user, assign it, and check the emails arrive.
4. Without Docker, set the app up as a [service](#run-it-as-a-service-without-docker) so it runs permanently.

---

## Configuration reference (.env)

The app reads `.env` from its own folder. Real environment variables override `.env`.

| Variable | Required | Default | What it does |
|---|:-:|---|---|
| `APP_PORT` | | `8000` | Docker: port opened on the server for the app |
| `TZ` | | `Asia/Kolkata` | Docker: time zone for ticket times in the app and emails |
| `HOST` | | `127.0.0.1` | Without Docker: address to listen on. Use `0.0.0.0` so other machines can connect |
| `PORT` | | `8000` | Without Docker: port for the web app and API |
| `APP_URL` | ✅ | `http://127.0.0.1:8000` | Public address of the app, used for links in emails |
| `SECRET_KEY` | ✅ | (insecure default) | Signs login tokens. Long random string. Changing it logs everyone out |
| `DATABASE_URL` | ✅ without Docker | | `postgresql://USER:PASSWORD@127.0.0.1:5434/DB`. Docker builds it automatically |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | ✅ | | Create the database. Without Docker they must match `DATABASE_URL`. Password: letters and numbers only |
| `ADMIN_EMAILS` | ✅ | (none) | Comma-separated admin addresses, applied on every start. `ADMIN_EMAIL` (one address) still works too |
| `ADMIN_PASSWORD` | | (empty) | Starting password only for admin accounts that don't exist yet. Never changes an existing password |
| `SMTP_HOST` / `SMTP_PORT` | | | Mail server, e.g. `smtp.office365.com` / `587` (STARTTLS). Port `465` uses SSL |
| `SMTP_USER` / `SMTP_PASS` | | | Mailbox login. Microsoft 365 and Gmail need an **app password** |
| `SMTP_FROM` | | `SMTP_USER` | Sender address shown on emails |
| `TEAM_EMAILS` | | | Comma-separated emails shown in the admin's **Assign** dropdown |

Without SMTP settings, emails are printed to the console and the sign-up code is shown on screen. That's fine for local testing, but not for production.

---

## Admin accounts

### How admins are set up

On **every start** (and whenever `--ensure-admins` runs) the app reads `ADMIN_EMAILS` and makes sure each address is an admin:

| The address in `ADMIN_EMAILS`… | What happens |
|---|---|
| already has an account (User or MLOps Engineer) | It is **promoted to admin**. Its password is not touched |
| already is an admin | Nothing |
| has no account, and `ADMIN_PASSWORD` is set | An admin account is created with `ADMIN_PASSWORD` as its starting password |
| has no account, and `ADMIN_PASSWORD` is empty | An admin account is created **without a password**. The owner activates it with *Create account*, using a code emailed to that address |

- **Admins not in the list:** left alone. Removing an address from `ADMIN_EMAILS` doesn't demote it; use *Users & Roles* for that.
- **Nothing hard-coded:** nobody becomes an admin by default. People who sign up are always Users.
- **Missing admins:** if the database ends up with no admin at all, the log says so and explains how to fix it.
- **Roles apply at once:** a role change takes effect on the user's next click, with no re-login. The role is read from the database on every request.
- **Every change is recorded:** each promotion or creation is written to the `audit_log` table.

### Production setup for this project

In the server's `.env`:

```ini
ADMIN_EMAILS=chaigrafana_alerts@iitmpravartak.net,admin@example.com
ADMIN_PASSWORD=Admin@123
```

Then, straight after deploying:

1. Log in as `admin@example.com` / `Admin@123`, open **Settings → Change Password** (bottom of the menu), and set a strong password. `admin@example.com` has no real mailbox, so *Forgot password?* can't work for it; Change Password is the only way.
2. Log in as `chaigrafana_alerts@iitmpravartak.net` with its existing password and change it the same way. **In the 15 Oct backups this account's password is still `Admin@123`.**
3. Remove the `ADMIN_PASSWORD` line from `.env` (or leave it empty) and restart: `docker compose up -d`.

Until an admin's password is changed, every start logs `Admin ... still uses the default password Admin@123`.

### Repair admin roles

Run this when someone who should be an admin shows up as a User. It applies `ADMIN_EMAILS` straight away and lists the admins, without restarting the app. It's safe to run any number of times.

```bash
# Docker (Linux or Windows PowerShell)
docker compose exec app python mlops_service_desk.py --ensure-admins

# Without Docker
python mlops_service_desk.py --ensure-admins
```

Expected output ends with `Admins now: chaigrafana_alerts@iitmpravartak.net, admin@example.com`. If an address you expect is missing, check its spelling in `.env`, then run `docker compose up -d` so the container picks up the edited `.env`, and run the command again.

Check the roles directly in the database:

```bash
docker exec servicedesk-postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "select email, role, pw_hash is not null as can_log_in from users where role='admin';"
```

**Emergency, if the app can't run at all:** promote an existing account by hand. This changes only the role; the password stays the same.

```bash
docker exec servicedesk-postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "update users set role='admin', updated_at=now() where email='chaigrafana_alerts@iitmpravartak.net';"
```

**Locked out of every admin account:** add an address you can receive mail at to `ADMIN_EMAILS`, run the repair command, then use *Forgot password?* (or *Create account* if it's new) with that address.

### Verify admin access

1. Log in as each admin. The menu shows **All Tickets** and **Users & Roles**.
2. Log in as a normal User. Those menu items are absent. Hiding them isn't the protection: the server checks the role on every admin request and answers `403 Admin only` to anyone else.

---

## Run it as a service (without Docker)

With Docker this isn't needed, because `restart: unless-stopped` brings both containers back after a reboot.

### Linux (systemd)

Assuming the project is in `/opt/Ticketing_Platform`, create `/etc/systemd/system/servicedesk.service`:

```ini
[Unit]
Description=MLOps Service Desk
After=network-online.target docker.service
Wants=network-online.target

[Service]
WorkingDirectory=/opt/Ticketing_Platform
ExecStart=/opt/Ticketing_Platform/.venv/bin/python mlops_service_desk.py
Restart=always
RestartSec=5
User=servicedesk

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now servicedesk
sudo journalctl -u servicedesk -f       # live logs
```

### Windows (Task Scheduler)

Run as Administrator. This starts the app at boot:

```powershell
schtasks /Create /TN "MLOps Service Desk" /SC ONSTART /RU SYSTEM /RL HIGHEST `
  /TR "C:\Ticketing_Platform\.venv\Scripts\python.exe C:\Ticketing_Platform\mlops_service_desk.py"
```

### HTTPS (recommended)

The app speaks plain HTTP. For HTTPS, put a reverse proxy such as nginx or IIS in front of it, keep `HOST=127.0.0.1`, and set `APP_URL` to the `https://` address. A minimal nginx example:

```nginx
server {
    listen 443 ssl;
    server_name servicedesk.yourcompany.com;
    ssl_certificate     /etc/ssl/servicedesk.crt;
    ssl_certificate_key /etc/ssl/servicedesk.key;
    client_max_body_size 40m;                  # screenshots are uploaded inside the request
    location / { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
}
```

---

## Updating, and rolling back

### Update (Docker)

```bash
cd ticketing_platform
# 1. Back up the database (see Backup and restore)
# 2. Keep the current image so you can go back to it
docker image tag mlops-servicedesk:latest mlops-servicedesk:rollback
git rev-parse --short HEAD          # note this commit id
# 3. Get the new code and rebuild. The database container and its volume are kept.
git pull
docker compose up -d --build
docker compose ps                   # both "healthy"
docker compose logs --tail 30 app   # look for "Connected to PostgreSQL" and no errors
```

- **Database:** changes are applied automatically on startup. They only add tables or columns, so no manual migrations are needed and nothing is deleted.
- **Users with the app open:** they see an *"A new version is available. Reload"* banner.
- **Never run `docker compose down -v`:** the `-v` deletes the database volume.

### Roll back (Docker)

```bash
docker image tag mlops-servicedesk:rollback mlops-servicedesk:latest
docker compose up -d --no-build app
git checkout <commit id noted above>     # keeps the files in step with the running image
```

The older version runs fine against the newer database, because updates only ever add columns. Restore the backup only if the data itself must go back to how it was before the update.

### Update without Docker

```bash
git pull
pip install -r requirements.txt     # inside the virtual environment
sudo systemctl restart servicedesk  # or stop and start the process
```

To roll back, `git checkout <previous commit>` and restart.

---

## Backup and restore

All data (users, tickets, history, comments, screenshots, notifications, audit log) is in PostgreSQL. Below, replace `servicedesk` with your `POSTGRES_USER` and `POSTGRES_DB`. These commands work the same in Linux and Windows PowerShell, because they copy files with `docker cp` instead of `<` / `>` redirection, which PowerShell mangles.

### Back up

```bash
docker exec servicedesk-postgres pg_dump -U servicedesk -d servicedesk -Fc -f /tmp/backup.dump
docker cp servicedesk-postgres:/tmp/backup.dump ./servicedesk-backup.dump
```

- **Format:** `-Fc` is PostgreSQL's custom format: compressed, and restored with `pg_restore`.
- **The file:** holds user emails and password hashes. Store it privately. Git and Docker already ignore `*.sql`, `*.dump`, `*.backup` and `backups/`.
- **Schedule:** run this daily (cron or Task Scheduler) with a dated file name, and copy the files off the server.

### Validate a backup before using it

Always restore into a **scratch** database first. This never touches the live data.

```bash
docker exec servicedesk-postgres psql -U servicedesk -d postgres -c "create database restore_check;"

# custom format (file starts with PGDMP, e.g. backups/servicedesk.backup)
docker cp ./servicedesk-backup.dump servicedesk-postgres:/tmp/restore.dump
docker exec servicedesk-postgres pg_restore -U servicedesk -d restore_check --no-owner --no-privileges --exit-on-error /tmp/restore.dump

# plain SQL format (file starts with "-- PostgreSQL database dump", e.g. servicedesk_dump_2026-10-15.sql)
docker cp ./servicedesk_dump_2026-10-15.sql servicedesk-postgres:/tmp/restore.sql
docker exec servicedesk-postgres psql -U servicedesk -d restore_check -v ON_ERROR_STOP=1 -q -f /tmp/restore.sql

# look inside, then drop the scratch database
docker exec servicedesk-postgres psql -U servicedesk -d restore_check -c "select email, role from users order by role, email;" -c "select count(*) as tickets from tickets;"
docker exec servicedesk-postgres psql -U servicedesk -d postgres -c "drop database restore_check;"
```

Both backups from 15 Oct 2026 (`servicedesk_dump_2026-10-15.sql` and `backups/servicedesk.backup`) were checked this way:

- **Contents:** full schema and data, with 4 users and 0 tickets.
- **Admin:** `chaigrafana_alerts@iitmpravartak.net` is the admin.
- **Compatibility:** they lack the newer `branch` column, which the app adds by itself on startup.

### Restore over the live database

Only do this to replace the live data on purpose. Take a fresh backup first.

```bash
docker compose stop app
docker exec servicedesk-postgres psql -U servicedesk -d postgres -c "drop database servicedesk;" -c "create database servicedesk;"
docker cp ./servicedesk-backup.dump servicedesk-postgres:/tmp/restore.dump
docker exec servicedesk-postgres pg_restore -U servicedesk -d servicedesk --no-owner --no-privileges --exit-on-error /tmp/restore.dump
# (plain SQL instead: docker cp the .sql file, then psql -v ON_ERROR_STOP=1 -f /tmp/restore.sql as above)
docker compose start app        # upgrades the schema and applies ADMIN_EMAILS on start
```

Changing a role never needs a restore. Use the [repair command](#repair-admin-roles) instead.

### SQL prompt

```bash
docker exec -it servicedesk-postgres psql -U servicedesk -d servicedesk
```

---

## Logs

| What | Command |
|---|---|
| App, live | `docker compose logs -f app` |
| App, last 100 lines | `docker compose logs --tail 100 app` |
| Database | `docker compose logs --tail 100 postgres` |
| Health of both | `docker compose ps` |
| App without Docker | the terminal it runs in, or `sudo journalctl -u servicedesk -f` |

Useful lines to look for:

- `Connected to PostgreSQL at postgresql://user:***@postgres:5432/...`. The password is always masked.
- `Created admin account ...` or `Promoted ... to admin`.
- `There is no admin account`.
- `still uses the default password`.
- `--- EMAIL to ...` for each email sent, and `Mail failed:` with the SMTP error when sending doesn't work.

---

## Security checklist

- [ ] `.env` is not in Git (`.gitignore` covers it), and only the service account can read it.
- [ ] `SECRET_KEY` is a long random value, not the example.
- [ ] Every admin has changed away from `Admin@123` (the startup log no longer warns), and `ADMIN_PASSWORD` is cleared in `.env`.
- [ ] `ADMIN_EMAILS` lists exactly the people who should be admins.
- [ ] `POSTGRES_PASSWORD` is strong. PostgreSQL stays bound to `127.0.0.1` (as in `docker-compose.yml`).
- [ ] The SMTP password is an app password, not the mailbox's main password.
- [ ] Port 8000 is open only to the office network, or HTTPS is set up through a reverse proxy.
- [ ] Daily backups are scheduled.

Protections already built into the app:

- **Passwords:** stored as salted PBKDF2-SHA256 hashes.
- **Email codes:** stop working after 5 wrong guesses, and only one can be sent per 30 seconds.
- **Access checks:** every API call checks the user's role and whether they're allowed to see that ticket.
- **Screenshots:** must be real PNG, JPG, GIF or WebP files, 5 MB each, at most 5 per ticket.

---

## Troubleshooting

| Problem | Cause and fix |
|---|---|
| `docker compose up` says `port is already allocated` | Something else uses port 8000 (for example a copy started with `python`). Stop it, or set `APP_PORT` in `.env` |
| App container keeps restarting | Read `docker compose logs app`. Usually a wrong `POSTGRES_*` value in `.env` |
| `Port 8000 is already in use` (without Docker) | Another copy is running. Stop it, or change `PORT` |
| `Cannot connect to PostgreSQL` at startup | The database isn't running. Run `docker compose up -d` and check `docker compose ps` |
| "The database is unavailable right now" in the app | PostgreSQL stopped while the app was running. Start it again (`docker compose up -d`); if errors continue, restart the app |
| Blank white page | The browser can't reach `cdnjs.cloudflare.com`. Allow it in the proxy or firewall |
| Others can't open the app | `HOST` isn't `0.0.0.0`, or the firewall blocks the port, or the server's IP changed |
| Links in emails point to the wrong address | Fix `APP_URL` in `.env` and restart |
| Emails not arriving | Run `docker compose exec app python mlops_service_desk.py --test-email you@company.com` (without Docker: `python mlops_service_desk.py --test-email ...`) and read the error. Check the Junk folder |
| `535 Authentication unsuccessful` from Outlook | Use an app password, and make sure SMTP AUTH is enabled for the mailbox in Microsoft 365 |
| Users still see the old screen after an update | They press **Reload** on the banner, or F5 |

The app logs to `docker compose logs -f app` with Docker, or to the console (`journalctl` when it runs as a service) without it. Every email it sends is also printed there.

---

## Features

### Roles

| Action | User | MLOps Engineer | Admin |
|---|:-:|:-:|:-:|
| Raise tickets, see and comment on their own tickets | ✅ | ✅ | |
| See tickets assigned to them | | ✅ | ✅ |
| See every ticket | | | ✅ |
| Assign or reassign tickets, take ownership | | | ✅ |
| Start, hold, resume and close a ticket | owner only | owner only | owner only |
| Change user roles | | | ✅ |

- New sign-ups are **Users**.
- Assigning a ticket to someone makes them an **MLOps Engineer**. If they have no account yet, an invited account is created and they activate it with *Create account*.
- Admins can't change their own role, and someone who owns open tickets can't be demoted to User.

### Request types

| Type | Extra fields |
|---|---|
| Deployment Request | Repository and Branch Name (required), Pull Request URL (optional) |
| Bug Fix | Repository (required), Pull Request URL (optional), up to 5 screenshots |
| Access Request | Access to (Server/SSH, Git, Database, AWS, Kubernetes, VPN, Grafana, Other), server or system, access level, needed until, access for |
| Other | Up to 5 screenshots |

### Ticket workflow

```
NEW ──assign / take ownership──▶ ASSIGNED ──Start Work──▶ IN PROGRESS ◀──Resume── ON HOLD
                                     │                        │  └──Put On Hold (reason)──┘
                                     └────────────Close Ticket (owner only)─────────────▶ CLOSED
```

- **Closing:** most tickets record the deployment status, date/time, notes, resolution and URL. Access requests record the result (Granted, Partially Granted or Rejected), the access given and instructions for the user.
- **Tracker tab:** hover over (or tap) the line between two steps to see how long that step took. On-hold time is shown separately.
- **Audit History tab:** every action is listed, with who did it and when.

### Notifications

- **Emails:** sent at every step to the requester, the owner, admins, anyone in *Also notify*, and the people an access request is for.
- **In-app:** notifications refresh every 5 seconds with an optional chime.
- **Dashboards:** each role has its own, refreshed every 10 seconds.

### Settings

**Settings** sits at the bottom of the menu, above Logout. It has two parts:

- **Appearance:** Light, Dark or System (follows the computer's setting). The choice is remembered per browser.
- **Change Password:** current password, then the new one twice.

### Command-line tools

```bash
python mlops_service_desk.py --test-email you@company.com     # check the SMTP settings
python mlops_service_desk.py --import-sqlite old.db           # one-time import of users from the old SQLite version
```

---

## Database tables

| Table | Holds |
|---|---|
| `users` | Accounts, roles (`user`, `engineer`, `admin`) and password hashes. No hash means an invited account |
| `tickets` | Every ticket: type, priority, environment, repository, PR URL, access details (JSON), status, owner |
| `ticket_resolutions` | Closure details for each closed ticket |
| `ticket_history` | Audit trail: every assignment, status change, comment and closure |
| `comments` | Conversation messages on tickets |
| `ticket_images` | Screenshots attached to Bug Fix and Other tickets |
| `notifications` | In-app notifications per user |
| `audit_log` | Role changes and invited users |
| `otps` | Pending email verification codes (expire after 10 minutes) |

Status changes lock the ticket row, so two people can't change the same ticket at once. Multi-step changes run in one transaction, so nothing is half-saved.
