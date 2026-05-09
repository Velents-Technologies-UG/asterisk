#!/bin/sh
# Asterisk container entrypoint.
#
# 1. Render any *.conf.template files in /etc/asterisk via envsubst.
#    This lets ConfigMaps reference env vars (ARI_PASSWORD, ODBC creds,
#    external IP) without committing real values to git.
# 2. Ensure runtime dirs exist + are writable (PVC mounts may replace
#    them empty).
# 3. exec Asterisk in foreground, dropping to the `asterisk` user when
#    started as root (the standard non-K8s path). When already running
#    as non-root (e.g. K8s securityContext.runAsUser=1000), skip the
#    -U/-G drop because Asterisk rejects those flags off-root.

set -eu

# 1. Templates -> .conf via envsubst. Loop is no-op if no templates.
for tmpl in /etc/asterisk/*.conf.template; do
  [ -f "$tmpl" ] || continue
  out="${tmpl%.template}"
  envsubst < "$tmpl" > "$out"
  chmod 640 "$out" 2>/dev/null || true
  echo "entrypoint: rendered $tmpl -> $out"
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

# 3. Control API sidecar. Listens on 8092 inside the pod for /healthz
# and /control/*. agent-hub (GCP) reaches this via the asterisk.velents.ai
# ingress; the path inside the pod is plain HTTP on 127.0.0.1:8092.
# Backgrounded so we still exec asterisk in the foreground; tini -g in
# the Dockerfile fans SIGTERM out to both processes on container stop.
if [ -x /usr/local/bin/control-api ]; then
  /usr/local/bin/control-api &
  echo "entrypoint: launched control-api on :${CONTROL_API_PORT:-8092} (pid $!)"
fi

# 4. Run asterisk in the foreground.
if [ "$(id -u)" = 0 ]; then
  exec /usr/sbin/asterisk -f -U asterisk -G asterisk -vvv "$@"
else
  exec /usr/sbin/asterisk -f -vvv "$@"
fi
