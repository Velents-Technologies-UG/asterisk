#!/usr/bin/env python3
"""Render unixODBC config from DATABASE_URL at container start.

Writes:
  /etc/odbc.ini
  /root/.odbc.ini
  /var/lib/asterisk/.odbc.ini    (chowned to asterisk:asterisk)
  /etc/asterisk/res_odbc.conf

The three odbc.ini paths carry the same [asterisk-pgsql] DSN with
URL-decoded creds. unixODBC searches $ODBCINI, then $HOME/.odbc.ini,
then /etc/odbc.ini — since Asterisk runs as user `asterisk` with
HOME=/var/lib/asterisk, the home-file takes precedence. We populate
all three so a stale or empty home-file never masks the real config.

res_odbc.conf carries inline `username` and `password` lines as well
as the dsn link. The historical `%`-mangling concern (which made us
strip creds from res_odbc.conf at one point) is moot here: `user`
and `pwd_str` below are URL-decoded by p.unquote() before they ever
reach the config file, so the literal `%` characters that confused
Asterisk's config_options parser are no longer present.

Defensive write: catches OSError, fsyncs each file, reads it back,
and verifies the [DSN] section header is the first non-blank line.
If a ConfigMap volume mount or any other layer is masking the file
after we write, the verify step logs a WARNING with the actual
first line so the symptom is obvious in kubectl logs.

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

# Default to sslmode=disable: DevOps configured RDS to accept
# unencrypted connections from the cluster (the asterisk pod sits
# inside the same VPC as the database, so the segment is already
# isolated). When this changes — e.g. a new cluster where RDS only
# accepts SSL — set PG_SSLMODE=require in the pod env to flip it back.
sslmode = os.environ.get("PG_SSLMODE", "disable")

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

EXPECTED_HEADER = f"[{DSN}]"


def _write_and_verify(path, content, expected_first_line):
    """Write content to path, fsync, then read back and check the header.

    Returns True on success, False on any write or verification failure.
    Logs progress to stderr so the symptom of a masked write is visible
    in kubectl logs.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"render_odbc: FAILED to write {path}: {exc}", file=sys.stderr)
        return False
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass
    try:
        with open(path) as f:
            actual = f.read()
    except OSError as exc:
        print(f"render_odbc: WARNING cannot read back {path}: {exc}",
              file=sys.stderr)
        return False
    # First non-blank line.
    first = next((ln.strip() for ln in actual.splitlines() if ln.strip()), "")
    size = len(actual)
    if first != expected_first_line:
        print(
            f"render_odbc: WARNING {path} verification failed: "
            f"first non-blank line is {first!r}, expected {expected_first_line!r}; "
            f"size={size}. Something (ConfigMap mount? init container?) is "
            f"masking the file.",
            file=sys.stderr,
        )
        return False
    print(f"render_odbc: wrote {path} ({size} bytes, header OK)",
          file=sys.stderr)
    return True


for path in ODBC_INI_PATHS:
    _write_and_verify(path, odbc_ini, EXPECTED_HEADER)

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

# res_odbc.conf — includes URL-decoded creds inline.
#
# WARNING about future-proofing: Asterisk's config_options parser
# historically mangles literal `%` characters in `password =>` lines.
# We avoid the hazard here because pwd_str is URL-decoded by
# p.unquote() above (any %XX sequences from the DATABASE_URL form
# have already become their literal chars by this point). If a future
# deployment ever needs to inject a password with literal `%`
# characters that are NOT URL-encoded in DATABASE_URL, this file
# would need different escaping — but that's not a scenario we hit
# today.
res_odbc = f"""[general]

[asterisk]
enabled => yes
dsn => {DSN}
username => {user}
password => {pwd_str}
pre-connect => yes
sanitysql => SELECT 1
backslash_is_escape => yes
max_connections => 5
"""
_write_and_verify(RES_ODBC_PATH, res_odbc, "[general]")

print(
    f"render_odbc: target={user}@{host}:{port}/{db} dsn={DSN} sslmode={sslmode}",
    file=sys.stderr,
)
