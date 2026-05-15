#!/usr/bin/env python3
"""Render unixODBC config from DATABASE_URL at container start.

Writes:
  /etc/odbc.ini
  /root/.odbc.ini
  /var/lib/asterisk/.odbc.ini    (chowned to asterisk:asterisk)

All three carry the same [asterisk-pgsql] DSN with URL-decoded
creds. unixODBC searches $ODBCINI, then $HOME/.odbc.ini, then
/etc/odbc.ini — since Asterisk runs as user `asterisk` with
HOME=/var/lib/asterisk, the home-file takes precedence. We populate
all three so a stale or empty home-file never masks the real config.

Also writes /etc/asterisk/res_odbc.conf with the dsn -> asterisk-pgsql
link, intentionally WITHOUT username/password lines. We learned the
hard way during debug that Asterisk's res_odbc config parser mangles
literal `%` characters in the password field; routing creds through
odbc.ini (which the unixODBC C parser handles correctly) sidesteps
that bug. res_odbc.conf falls back to the DSN's stored creds when no
username/password override is present — the documented behavior.

The Python ODBC driver doesn't URL-decode credentials the way libpq
does implicitly for postgresql:// URLs, so we unquote() here.

Stdlib only (no pip).
"""
import os
import pwd
import sys
import urllib.parse as p

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DSN = os.environ.get("ODBC_DSN_NAME", "asterisk-pgsql")

ODBC_INI_PATHS = [
    os.environ.get("ODBC_INI_PATH", "/etc/odbc.ini"),
    "/root/.odbc.ini",
    "/var/lib/asterisk/.odbc.ini",
]
RES_ODBC_PATH = "/etc/asterisk/res_odbc.conf"

if not DATABASE_URL:
    print("render_odbc: DATABASE_URL unset; skipping render", file=sys.stderr)
    sys.exit(0)

u = p.urlparse(DATABASE_URL)
if u.scheme not in ("postgres", "postgresql"):
    print(f"render_odbc: DATABASE_URL scheme {u.scheme!r} unsupported; skipping",
          file=sys.stderr)
    sys.exit(0)

host = u.hostname or "localhost"
port = u.port or 5432
db   = (u.path or "/").lstrip("/")
user = p.unquote(u.username or "")
pwd_str = p.unquote(u.password or "")

# RDS and other managed Postgres reject unencrypted connections.
# Empirically confirmed during debug: sslmode=disable produced
# `no pg_hba.conf entry for host ..., no encryption`. Default to
# require; override via PG_SSLMODE only for local docker-compose
# dev where TLS is off.
sslmode = os.environ.get("PG_SSLMODE", "require")

odbc_ini = f"""[{DSN}]
Description = Asterisk PJSIP realtime
Driver = PostgreSQL Unicode
Servername = {host}
Port = {port}
Database = {db}
Username = {user}
Password = {pwd_str}
Protocol = 11
SSLmode = {sslmode}
TextAsLongVarchar = 0
UnknownsAsLongVarchar = 0
BoolsAsChar = 0
Padding = 0
"""

for path in ODBC_INI_PATHS:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    with open(path, "w") as f:
        f.write(odbc_ini)
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass
    print(f"render_odbc: wrote {path}", file=sys.stderr)

# The asterisk-home copy needs to be readable by the asterisk user.
# render_odbc runs early in entrypoint.sh as root; chown so the
# subsequent asterisk -U asterisk -G asterisk drop doesn't lose
# read access.
try:
    pw = pwd.getpwnam("asterisk")
    os.chown("/var/lib/asterisk/.odbc.ini", pw.pw_uid, pw.pw_gid)
except (KeyError, OSError) as exc:
    print(f"render_odbc: chown asterisk home odbc.ini skipped: {exc}",
          file=sys.stderr)

# res_odbc.conf — minimal, no credentials. Letting res_odbc fall
# through to the DSN's stored creds avoids the `%`-in-password
# mangling we hit during the debug session.
res_odbc = f"""[general]

[asterisk]
enabled => yes
dsn => {DSN}
pre-connect => yes
sanitysql => SELECT 1
backslash_is_escape => yes
max_connections => 5
"""
with open(RES_ODBC_PATH, "w") as f:
    f.write(res_odbc)
try:
    os.chmod(RES_ODBC_PATH, 0o640)
except OSError:
    pass
print(f"render_odbc: wrote {RES_ODBC_PATH} (no inline creds; falls through to DSN)",
      file=sys.stderr)

print(
    f"render_odbc: target={user}@{host}:{port}/{db} dsn={DSN} sslmode={sslmode}",
    file=sys.stderr,
)
