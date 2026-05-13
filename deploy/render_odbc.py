#!/usr/bin/env python3
"""Render /etc/odbc.ini from DATABASE_URL at container start.

The PostgreSQL ODBC driver doesn't speak postgresql:// URLs natively;
it needs explicit Servername / Port / Database / Username / Password
fields in /etc/odbc.ini. Rather than ship a static file with creds
baked in (or rely on a ConfigMap that DevOps has to keep in sync with
the Postgres role), parse DATABASE_URL here and write the file at
startup.

This mirrors the manual python one-liner used to repair the live pod
during the debug session that produced PR #11 — baking it into the
image means a pod restart never loses the realtime ODBC connection.

Stdlib only (no pip).
"""
import os
import sys
import urllib.parse as p

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
OUT = os.environ.get("ODBC_INI_PATH", "/etc/odbc.ini")
DSN = os.environ.get("ODBC_DSN_NAME", "asterisk-pgsql")

if not DATABASE_URL:
    print("render_odbc: DATABASE_URL unset; skipping /etc/odbc.ini render",
          file=sys.stderr)
    sys.exit(0)

u = p.urlparse(DATABASE_URL)
if u.scheme not in ("postgres", "postgresql"):
    print(f"render_odbc: DATABASE_URL scheme {u.scheme!r} unsupported; skipping",
          file=sys.stderr)
    sys.exit(0)

host = u.hostname or "localhost"
port = u.port or 5432
db   = (u.path or "/").lstrip("/")
# urlparse leaves username/password URL-encoded; unquote both so the
# ODBC driver sees the literal credentials Postgres expects. (libpq
# does this implicitly when fed a postgresql:// URL; the ODBC driver
# does not.)
user = p.unquote(u.username or "")
pwd  = p.unquote(u.password or "")

# Managed Postgres (AWS RDS, etc.) refuses unencrypted connections
# from outside the VPC. Default to require; override via PG_SSLMODE
# for local / docker-compose dev where TLS is off.
sslmode = os.environ.get("PG_SSLMODE", "require")

content = f"""[{DSN}]
Description = Asterisk PJSIP realtime
Driver = PostgreSQL Unicode
Servername = {host}
Port = {port}
Database = {db}
Username = {user}
Password = {pwd}
Protocol = 11
SSLmode = {sslmode}
TextAsLongVarchar = 0
UnknownsAsLongVarchar = 0
BoolsAsChar = 0
Padding = 0
"""

with open(OUT, "w") as f:
    f.write(content)
try:
    os.chmod(OUT, 0o640)
except OSError:
    pass  # mount may be read-only-after-write; not fatal

print(
    f"render_odbc: wrote {OUT} DSN={DSN} target={user}@{host}:{port}/{db} sslmode={sslmode}",
    file=sys.stderr,
)
