#!/bin/sh
# Asterisk container entrypoint.
#
# 1. Compute env-derived values that feed into envsubst (NAT block,
#    anything else that needs shell-side conditionals).
# 2. Render /etc/odbc.ini from DATABASE_URL (so the realtime ODBC
#    connection survives a pod restart without a hand-patched file).
# 3. Render any *.conf.template files in /etc/asterisk via envsubst.
#    This lets ConfigMaps reference env vars (ARI_PASSWORD, ODBC creds,
#    external IP) without committing real values to git.
# 4. Ensure runtime dirs exist + are writable (PVC mounts may replace
#    them empty).
# 5. Generate a self-signed TLS keypair at /etc/asterisk/keys/asterisk.pem
#    if one isn't already present. The TLS transport (port 5062) needs
#    a cert at boot; without it pjsip refuses to bind the transport.
#    A real CA cert (Let's Encrypt etc.) shipped via secret will mask
#    this file at mount time, so this is a fallback for dev / first-
#    boot only.
# 6. Bring up the call-engine control-api sidecar on :8092 BEFORE
#    asterisk. We pre-flight (python3 + script present), launch it
#    under a supervisor that restarts on crash, then poll /healthz
#    until it answers. If the sidecar can't bind we fail the whole
#    container so k8s shows a clear startup error instead of letting
#    asterisk run on its own and serving Connection refused on 8092.
# 7. Mirror the entire /var/lib/asterisk/documentation/ directory into
#    /usr/share/asterisk/documentation/ so res_pjsip and other modules
#    find core-en_US.xml plus its sibling _common.xml / DTD when they
#    register option XSDs at startup. Per-file ln -sf so we don't
#    depend on directory-target symlinks.
# 8. exec Asterisk in foreground, dropping to the `asterisk` user when
#    started as root (the standard non-K8s path). When already running
#    as non-root (e.g. K8s securityContext.runAsUser=1000), skip the
#    -U/-G drop because Asterisk rejects those flags off-root.

set -eu

CONTROL_API_PORT="${CONTROL_API_PORT:-8092}"
CONTROL_API_BIN=/usr/local/bin/control-api
CONTROL_API_READY_TIMEOUT="${CONTROL_API_READY_TIMEOUT:-10}"
RENDER_ODBC_BIN=/usr/local/bin/render-odbc
TLS_CERT_PATH="${ASTERISK_TLS_CERT_PATH:-/etc/asterisk/keys/asterisk.pem}"
TLS_CERT_CN="${ASTERISK_TLS_CERT_CN:-asterisk.velents.ai}"

log() { echo "entrypoint: $*"; }

# 1. NAT-aware transport block.
#
# DevOps sets ASTERISK_EXTERNAL_IP to the public address the carrier
# sees when this pod opens an outbound TLS connection (i.e. the
# cluster's NAT-egress IP, or the LoadBalancer's external IP). When
# present, the rendered transport-tls block tells Asterisk to rewrite
# its outbound SIP Contact / Via headers and SDP c= lines to that IP
# instead of the pod's internal 10.x.x.x. Without it, the carrier
# accepts our REGISTER but cannot route inbound INVITEs or RTP back.
#
# ASTERISK_LOCAL_NET (default RFC1918) tells Asterisk NOT to apply
# the NAT rewrite when talking to peers on the LAN — important for
# in-cluster traffic to the agent-hub call-engine sidecar.
if [ -n "${ASTERISK_EXTERNAL_IP:-}" ]; then
  ASTERISK_TLS_NAT_BLOCK="external_media_address=${ASTERISK_EXTERNAL_IP}
external_signaling_address=${ASTERISK_EXTERNAL_IP}
local_net=${ASTERISK_LOCAL_NET:-10.0.0.0/8}"
  if [ -n "${ASTERISK_LOCAL_NET2:-}" ]; then
    ASTERISK_TLS_NAT_BLOCK="${ASTERISK_TLS_NAT_BLOCK}
local_net=${ASTERISK_LOCAL_NET2}"
  fi
  if [ -n "${ASTERISK_LOCAL_NET3:-}" ]; then
    ASTERISK_TLS_NAT_BLOCK="${ASTERISK_TLS_NAT_BLOCK}
local_net=${ASTERISK_LOCAL_NET3}"
  fi
  log "NAT-aware transport: external IP=${ASTERISK_EXTERNAL_IP}"
else
  ASTERISK_TLS_NAT_BLOCK=""
  log "no ASTERISK_EXTERNAL_IP set; SIP will advertise pod internal IP (OK only on host-network)"
fi
export ASTERISK_TLS_NAT_BLOCK

# 2. /etc/odbc.ini from DATABASE_URL.
#
# unixODBC needs explicit Servername/Port/Database/Username/Password
# in /etc/odbc.ini for the [asterisk-pgsql] DSN to resolve. Without
# this step the file is empty on a fresh pod and Asterisk's realtime
# engine can't load ps_endpoints / ps_aors / ps_auths / ps_registrations
# — trunks created in the UI exist in the DB but Asterisk never sees
# them. render-odbc no-ops if DATABASE_URL isn't set, so non-realtime
# deployments aren't affected.
if [ -r "$RENDER_ODBC_BIN" ]; then
  python3 "$RENDER_ODBC_BIN" || log "WARNING: render-odbc exited non-zero; see message above"
else
  log "WARNING: $RENDER_ODBC_BIN missing; /etc/odbc.ini will not be regenerated"
fi

# 3. Templates -> .conf via envsubst. Loop is no-op if no templates.
for tmpl in /etc/asterisk/*.conf.template; do
  [ -f "$tmpl" ] || continue
  out="${tmpl%.template}"
  envsubst < "$tmpl" > "$out"
  chmod 640 "$out" 2>/dev/null || true
  log "rendered $tmpl -> $out"
done

# 3b. Inject external_media_address / external_signaling_address into
# each PJSIP transport section when ASTERISK_EXTERNAL_MEDIA_ADDRESS is
# set. Needed when the pod sits behind a NAT/NLB: without this, the
# SDP that Asterisk hands to the carrier carries the pod's internal
# IP (10.x), the carrier RTPs there, packets get dropped, and the
# call tears down with no audio. The IP belongs in env (not the
# sample files) because it's deployment-specific — prod has a public
# NLB; test/dev don't.
#
# Sorcery rejects duplicate object definitions across includes, so
# the injection has to edit the transport's home file in-place rather
# than append a second [transport-udp] block elsewhere. sed is
# idempotent because we grep for the marker first.
if [ -n "${ASTERISK_EXTERNAL_MEDIA_ADDRESS:-}" ]; then
  EMA="$ASTERISK_EXTERNAL_MEDIA_ADDRESS"
  ESA="${ASTERISK_EXTERNAL_SIGNALING_ADDRESS:-$EMA}"
  for f in /etc/asterisk/pjsip_trunks.conf /etc/asterisk/pjsip_wss_agents.conf; do
    [ -f "$f" ] || continue
    if grep -q '^external_media_address=' "$f"; then
      log "external_media_address already present in $f, skipping"
      continue
    fi
    # One sed pass per transport header so the injection lands under
    # the right section even if a file declares more than one.
    for hdr in '[transport-udp]' '[transport-tcp]' '[transport-wss]'; do
      grep -qF "$hdr" "$f" || continue
      esc_hdr=$(printf '%s' "$hdr" | sed 's/[][\.*^$/]/\\&/g')
      sed -i "/^${esc_hdr}\$/a external_media_address=${EMA}\nexternal_signaling_address=${ESA}" "$f"
      log "injected external_media_address=${EMA} under ${hdr} in $f"
    done
  done
fi

# 4. Runtime dirs - idempotent. PVCs / emptyDirs may mask the image's
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

# 5. TLS cert (self-signed fallback).
#
# pjsip's TLS transport requires a cert file at boot; if it's missing
# Asterisk refuses to bind the transport and trunks on port 5061
# silently fall back to UDP (or fail outright). DevOps will normally
# mount a real cert into /etc/asterisk/keys/asterisk.pem via a
# Kubernetes Secret + volumeMount, which masks any file generated
# here. For dev/first-boot when no such secret exists, generate a
# 10-year self-signed cert so the transport at least binds and the
# pod can come up. Subject CN is configurable via env so DevOps can
# point it at the real hostname if needed.
if [ ! -s "$TLS_CERT_PATH" ]; then
  if command -v openssl >/dev/null 2>&1; then
    log "generating self-signed TLS cert at $TLS_CERT_PATH (CN=$TLS_CERT_CN)"
    openssl req -x509 -newkey rsa:2048 \
      -keyout "$TLS_CERT_PATH" \
      -out "$TLS_CERT_PATH" \
      -days 3650 -nodes \
      -subj "/CN=$TLS_CERT_CN" 2>&1 \
      | sed 's/^/[openssl] /' \
      || log "WARNING: openssl returned non-zero; TLS transport may fail to bind"
    chown asterisk:asterisk "$TLS_CERT_PATH" 2>/dev/null || true
    chmod 600 "$TLS_CERT_PATH" 2>/dev/null || true
  else
    log "WARNING: openssl missing and $TLS_CERT_PATH absent; TLS transport will fail to bind"
  fi
else
  log "TLS cert present at $TLS_CERT_PATH (size $(wc -c < "$TLS_CERT_PATH") bytes)"
fi

# 6. Control API sidecar.
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

# 7. XML doc mirror.
#
# Asterisk's xmldoc loader parses core-en_US.xml against appdocsxml.dtd
# and pulls sibling files (_common.xml, *.xsd, and any per-module XML)
# from the SAME directory. With our build flags (sysconfdir=/etc
# localstatedir=/var --prefix=/usr) `make install` drops the files
# under /var/lib/asterisk/documentation/, but res_pjsip et al. read
# from /usr/share/asterisk/documentation/. Symlinking only
# core-en_US.xml (the previous fix) wasn't enough because the DTD ref
# failed to resolve, which made res_pjsip_outbound_registration abort
# at sorcery type registration init. Mirror every relevant file.
mkdir -p /usr/share/asterisk/documentation
doc_count=0
for f in /var/lib/asterisk/documentation/*.xml \
         /var/lib/asterisk/documentation/*.xsd \
         /var/lib/asterisk/documentation/*.dtd; do
  [ -e "$f" ] || continue
  if ln -sf "$f" "/usr/share/asterisk/documentation/$(basename "$f")" 2>/dev/null; then
    doc_count=$((doc_count + 1))
  fi
done
log "stitched $doc_count XML doc files into /usr/share/asterisk/documentation/"

# 8. Run asterisk in the foreground.
if [ "$(id -u)" = 0 ]; then
  exec /usr/sbin/asterisk -f -U asterisk -G asterisk -vvv "$@"
else
  exec /usr/sbin/asterisk -f -vvv "$@"
fi
