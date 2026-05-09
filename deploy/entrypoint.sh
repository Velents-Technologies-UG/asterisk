#!/bin/sh
# Asterisk container entrypoint.
#
# 1. Render any *.conf.template files in /etc/asterisk via envsubst.
#    This lets ConfigMaps reference env vars (ARI_PASSWORD, ODBC creds,
#    external IP) without committing real values to git.
# 2. Ensure runtime dirs exist + are writable (PVC mounts may replace
#    them empty).
# 3. Bring up the call-engine control-api sidecar on :8092 BEFORE
#    asterisk. We pre-flight (python3 + script present), launch it
#    under a supervisor that restarts on crash, then poll /healthz
#    until it answers. If the sidecar can't bind we fail the whole
#    container so k8s shows a clear startup error instead of letting
#    asterisk run on its own and serving Connection refused on 8092.
# 4. exec Asterisk in foreground, dropping to the `asterisk` user when
#    started as root (the standard non-K8s path). When already running
#    as non-root (e.g. K8s securityContext.runAsUser=1000), skip the
#    -U/-G drop because Asterisk rejects those flags off-root.

set -eu

CONTROL_API_PORT="${CONTROL_API_PORT:-8092}"
CONTROL_API_BIN=/usr/local/bin/control-api
CONTROL_API_READY_TIMEOUT="${CONTROL_API_READY_TIMEOUT:-10}"

log() { echo "entrypoint: $*"; }

# 1. Templates -> .conf via envsubst. Loop is no-op if no templates.
for tmpl in /etc/asterisk/*.conf.template; do
  [ -f "$tmpl" ] || continue
  out="${tmpl%.template}"
  envsubst < "$tmpl" > "$out"
  chmod 640 "$out" 2>/dev/null || true
  log "rendered $tmpl -> $out"
done

# 2. Runtime dirs - idempotent. PVCs / emptyDirs may mask the image's
# pre-created versions, so re-create on every start.
for d in \
    /var/spool/asterisk/recording \
    /var/spool/asterisk/voicemail \
    /var/lib/asterisk/sounds/custom \
    /etc/asterisk/keys \
    /var/log/asterisk \
    /var/run/asterisk
do
  mkdir -p "$d"
  chown asterisk:asterisk "$d" 2>/dev/null || true
done

# 3. Control API sidecar.
#
# Pre-flight checks fail the container with a clear message rather
# than silently starting asterisk only — DevOps's prior debug session
# saw `Connection refused` on 127.0.0.1:8092 with no logs because the
# sidecar was launched with `&` and any startup error vanished into
# the background. The supervisor block below keeps it alive across
# crashes; the readiness probe verifies it's actually listening before
# we hand control to asterisk.
if ! command -v python3 >/dev/null 2>&1; then
  log "FATAL: python3 not found on PATH; control-api sidecar cannot start." >&2
  log "       rebuild the image (Dockerfile.dev/.prod install python3 explicitly)." >&2
  exit 1
fi

if [ ! -r "$CONTROL_API_BIN" ]; then
  log "FATAL: $CONTROL_API_BIN missing or unreadable; control-api sidecar cannot start." >&2
  log "       a ConfigMap mount on /usr/local/bin/ would mask it — check the pod spec." >&2
  exit 1
fi

# Supervisor: restart the sidecar on crash with a small backoff so a
# bug in control_api.py doesn't take down the whole pod's listener.
# Output is prefixed and forwarded to stdout so `kubectl logs` shows
# it interleaved with asterisk's own logs.
supervise_control_api() {
  while true; do
    python3 "$CONTROL_API_BIN" 2>&1 | sed -u 's/^/[control-api] /' || true
    rc=$?
    log "control-api exited (rc=$rc); restarting in 2s" >&2
    sleep 2
  done
}

supervise_control_api &
SUPERVISOR_PID=$!
log "control-api supervisor started (pid $SUPERVISOR_PID), target port :$CONTROL_API_PORT"

# Readiness probe — poll /healthz until we get a 2xx, up to N seconds.
# Uses curl (already in both images) so we don't need a netcat. We
# treat any non-2xx as not-ready so a failing python startup surfaces
# as a hard container failure rather than as 502s on the agent-hub
# trunks page later.
ready=0
i=0
while [ "$i" -lt "$CONTROL_API_READY_TIMEOUT" ]; do
  if curl -fsS -o /dev/null --max-time 1 "http://127.0.0.1:${CONTROL_API_PORT}/healthz" 2>/dev/null; then
    ready=1
    break
  fi
  i=$((i + 1))
  sleep 1
done

if [ "$ready" -ne 1 ]; then
  log "FATAL: control-api did not bind 127.0.0.1:${CONTROL_API_PORT} within ${CONTROL_API_READY_TIMEOUT}s." >&2
  log "       see [control-api] log lines above for the underlying python error." >&2
  exit 1
fi
log "control-api ready on :$CONTROL_API_PORT"

# 4. Run asterisk in the foreground.
if [ "$(id -u)" = 0 ]; then
  exec /usr/sbin/asterisk -f -U asterisk -G asterisk -vvv "$@"
else
  exec /usr/sbin/asterisk -f -vvv "$@"
fi
