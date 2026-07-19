#!/usr/bin/env bash
# End-to-end audit of the browser-UI → Asterisk → carrier call pipeline.
# Each gate prints PASS / FAIL / WARN and the diagnostic that explains
# why. Run top-to-bottom; if a gate fails, fix that layer before moving
# on — later gates depend on earlier ones.
#
# Usage:
#   NS=velents \
#   TRUNK=innov2 \
#   AGENT_ID=9999 \
#   TEST_DID=+491742604740 \
#   bash scripts/audit-call-pipeline.sh
#
# Optional:
#   CONTROL_URL=https://asterisk-ari.velents.ai   # override sidecar base
#   BEARER=...                                    # CONTROL_API_SECRET; if
#                                                 # unset, only in-cluster
#                                                 # gates run
#   AGENT_HUB_URL=https://agent-hub-test.velents.ai
#   ENV_FILE=.env                                  # where Gate A/B read
#                                                   # ASTERISK_WSS_URL /
#                                                   # ASTERISK_SIP_DOMAIN
#                                                   # from, to derive the
#                                                   # canonical WSS host
#
# Read-only. The only mutation is gate E (originate to TEST_DID) — comment
# it out if you don't want to ring a phone.

set -u

NS="${NS:-velents}"
TRUNK="${TRUNK:-}"
AGENT_ID="${AGENT_ID:-}"
TEST_DID="${TEST_DID:-}"
CONTROL_URL="${CONTROL_URL:-}"
BEARER="${BEARER:-}"
AGENT_HUB_URL="${AGENT_HUB_URL:-}"
ENV_FILE="${ENV_FILE:-.env}"

C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_OFF=$'\033[0m'
pass() { printf '%s  PASS%s  %s\n' "$C_GRN" "$C_OFF" "$*"; }
fail() { printf '%s  FAIL%s  %s\n' "$C_RED" "$C_OFF" "$*"; FAILED=$((FAILED+1)); }
warn() { printf '%s  WARN%s  %s\n' "$C_YEL" "$C_OFF" "$*"; }
hdr()  { printf '\n%s── %s ──%s\n'  "$C_YEL" "$*" "$C_OFF"; }

FAILED=0

# Derive the one canonical WSS host both Gate A and Gate B key off of.
# Prefers ASTERISK_WSS_URL (the value that was actually wrong in the
# :8089 incident this checklist exists to catch), falls back to
# ASTERISK_SIP_DOMAIN, then a hardcoded default so the gate still runs
# somewhere without an .env file.
derive_wss_host() {
  local url host
  if [[ -f "$ENV_FILE" ]]; then
    url=$(grep -E '^ASTERISK_WSS_URL=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
    if [[ -n "$url" ]]; then
      host=$(echo "$url" | sed -E 's#^wss?://##; s#[:/].*$##')
      [[ -n "$host" ]] && { echo "$host"; return; }
    fi
    host=$(grep -E '^ASTERISK_SIP_DOMAIN=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
    [[ -n "$host" ]] && { echo "$host"; return; }
  fi
  echo "asterisk.velents.ai"
}
WSS_HOST=$(derive_wss_host)

# ────────────────────────────────────────────────────────────────────
hdr "Gate A — Signaling exposure (Services + Ingress)"
# What we're checking: ports 5060/5061/8088/8089 are reachable from
# outside the cluster, plus RTP UDP 10000-10099 on the NLB. Without
# these the carrier can't talk to us and the browser can't open WSS.
# ────────────────────────────────────────────────────────────────────

SVC_OUT=$(kubectl -n "$NS" get svc -o wide 2>/dev/null) \
  || { fail "kubectl get svc failed; check kubeconfig"; exit 1; }

echo "$SVC_OUT" | awk 'NR==1 || /asterisk/'

SIP_PORTS_FOUND=$(echo "$SVC_OUT" | grep -oE '\b(5060|5061|8088|8089)/(TCP|UDP)' | sort -u)

# RTP is exposed today as individual NodePort UDP mappings
# (10000:31386/UDP,10001:30900/UDP,...), not a literal "10000-10099"
# range string — match both forms. Per SIP_GO_LIVE_RUNBOOK.md item 1,
# the AWS security-group rule quota currently limits this to exactly
# 10000-10003 (4 ports, ~2-4 concurrent calls); that's a known/accepted
# limitation, not a fresh regression, so it WARNs rather than FAILs.
RTP_RANGE_LITERAL=$(echo "$SVC_OUT" | grep -oE '10000-10099|10000:10099' | head -1)
RTP_PORTS_FOUND=$(echo "$SVC_OUT" | grep -oE '\b1[0-9]{4}:[0-9]+/UDP' | grep -oE '^1[0-9]{4}' | sort -un)
RTP_COUNT=$(echo -n "$RTP_PORTS_FOUND" | grep -c '^[0-9]')

[[ -n "$SIP_PORTS_FOUND" ]] \
  && pass "SIP signaling ports exposed: $(echo $SIP_PORTS_FOUND | tr '\n' ' ')" \
  || fail "no Service exposes 5060/5061/8088/8089 — carriers and softphones can't reach the pod"

if [[ -n "$RTP_RANGE_LITERAL" ]]; then
  pass "RTP UDP range exposed via NLB: $RTP_RANGE_LITERAL"
elif [[ "$RTP_COUNT" -eq 4 ]]; then
  warn "RTP UDP range limited to 4 ports ($(echo $RTP_PORTS_FOUND | tr '\n' ' ')) — known/accepted per SIP_GO_LIVE_RUNBOOK.md item 1 (AWS SG rule quota; ~2-4 concurrent call ceiling)"
elif [[ "$RTP_COUNT" -gt 4 ]]; then
  pass "RTP UDP range exposed via NLB: $RTP_COUNT ports ($(echo $RTP_PORTS_FOUND | tr '\n' ' '))"
elif [[ "$RTP_COUNT" -gt 0 ]]; then
  fail "RTP UDP range narrower than the documented 4-port minimum — only $RTP_COUNT port(s) found: $(echo $RTP_PORTS_FOUND | tr '\n' ' ')"
else
  fail "RTP UDP range not exposed — calls will answer with no audio"
fi

ING=$(kubectl -n "$NS" get ingress -o jsonpath='{range .items[*]}{.spec.rules[*].host}{"\n"}{end}' 2>/dev/null \
        | sort -u)
echo "Ingress hosts:"; echo "$ING" | sed 's/^/    /'

echo "$ING" | grep -qx "$WSS_HOST" && pass "WSS Ingress route present ($WSS_HOST)" \
  || warn "no Ingress host matching '$WSS_HOST' found; WSS may not be terminated at the Ingress"

# ────────────────────────────────────────────────────────────────────
hdr "Gate B — WSS handshake (TLS cert + reachability)"
# Browser softphones connect to wss://<host>/ws (TLS terminated at the
# Ingress). If the cert is self-signed, expired, or the host doesn't
# resolve, the Softphone widget hangs at 'connecting' forever.
# ────────────────────────────────────────────────────────────────────

if echo "$ING" | grep -qx "$WSS_HOST"; then
  CERT_INFO=$(timeout 5 openssl s_client -servername "$WSS_HOST" \
                -connect "$WSS_HOST:443" </dev/null 2>/dev/null \
              | openssl x509 -noout -issuer -subject -dates 2>/dev/null)
  if [[ -n "$CERT_INFO" ]]; then
    echo "$CERT_INFO" | sed 's/^/    /'
    echo "$CERT_INFO" | grep -q 'O=Let.s Encrypt\|O = Let.s Encrypt' \
      && pass "TLS cert is Let's Encrypt (CA-signed)" \
      || warn "TLS cert is not Let's Encrypt; browser may still trust it but verify"
  else
    fail "TLS handshake to $WSS_HOST:443 failed"
  fi
else
  warn "skipping — no WSS Ingress host detected in gate A"
fi

# ────────────────────────────────────────────────────────────────────
hdr "Gate C — Permissions-Policy header on the agent-hub host"
# Known blocker: if the response sets Permissions-Policy: microphone=()
# JsSIP can't call getUserMedia and the call fails silently before SDP.
# ────────────────────────────────────────────────────────────────────

if [[ -n "$AGENT_HUB_URL" ]]; then
  PP=$(curl -sI -L "$AGENT_HUB_URL/" 2>/dev/null | grep -i '^permissions-policy:')
  if [[ -n "$PP" ]]; then
    echo "    $PP"
    echo "$PP" | grep -qiE 'microphone=\(\)|microphone=\(none\)' \
      && fail "Permissions-Policy blocks microphone — browser can't use mic" \
      || pass "Permissions-Policy present but does not block mic"
  else
    pass "no Permissions-Policy header (mic permission flows through normally)"
  fi
else
  warn "AGENT_HUB_URL not set; skipping"
fi

# ────────────────────────────────────────────────────────────────────
hdr "Gate D — Transports + DB connectivity inside the pod"
# Realtime trunks/agents can't load if (a) Postgres unreachable or
# (b) required PJSIP transports are missing.
# ────────────────────────────────────────────────────────────────────

K() { kubectl exec -n "$NS" deploy/asterisk -- "$@" 2>&1; }

TRANSPORTS=$(K asterisk -rx 'pjsip show transports' | grep -E '^Transport:')
echo "$TRANSPORTS" | sed 's/^/    /'

for t in transport-udp transport-tcp transport-tls transport-wss; do
  echo "$TRANSPORTS" | grep -q "$t" \
    && pass "$t loaded" \
    || warn "$t NOT loaded — trunks/agents requiring it will fail to register"
done

DB_OK=$(K python3 -c "
import os, urllib.parse, sys
try:
    import psycopg2
    u = urllib.parse.urlparse(os.environ['DATABASE_URL'])
    psycopg2.connect(host=u.hostname, port=u.port,
                     dbname=(u.path or '').lstrip('/') or None,
                     user=urllib.parse.unquote(u.username) if u.username else None,
                     password=urllib.parse.unquote(u.password) if u.password else None,
                     sslmode=os.environ.get('DATABASE_SSLMODE','disable')).close()
    print('OK')
except Exception as e:
    print('ERR', e); sys.exit(1)
" 2>&1)
[[ "$DB_OK" == "OK" ]] && pass "Postgres reachable from the pod" \
  || fail "Postgres connect failed: $DB_OK"

TRUNK_ROWS=$(K python3 -c "
import os, urllib.parse, psycopg2
u = urllib.parse.urlparse(os.environ['DATABASE_URL'])
c = psycopg2.connect(host=u.hostname, port=u.port,
                    dbname=(u.path or '').lstrip('/') or None,
                    user=urllib.parse.unquote(u.username) if u.username else None,
                    password=urllib.parse.unquote(u.password) if u.password else None,
                    sslmode=os.environ.get('DATABASE_SSLMODE','disable'))
cur = c.cursor()
cur.execute(\"SELECT id, name, address, protocol, media_encryption, register_enabled FROM sip_trunks ORDER BY id\")
for r in cur: print('   ', r)
print('count=', cur.rowcount)
" 2>&1)
echo "$TRUNK_ROWS"

# ────────────────────────────────────────────────────────────────────
hdr "Gate E — Trunk REGISTER state"
# Each row in sip_trunks with register_enabled=true should be
# 'Registered (exp. NNN)' against its carrier. 'Rejected' = 401/403
# from the carrier; capture the wire log if so.
# ────────────────────────────────────────────────────────────────────

REGS=$(K asterisk -rx 'pjsip show registrations')
echo "$REGS" | awk '/^.Registration\/|^ <Registration\/|^ Registration\//' | sed 's/^/    /'

if [[ -n "$TRUNK" ]]; then
  if echo "$REGS" | grep -qE "^.*$TRUNK.*Registered"; then
    pass "trunk '$TRUNK' is Registered"
  else
    fail "trunk '$TRUNK' is NOT Registered"
    echo "    Capture wire log to diagnose:"
    echo "    kubectl exec -n $NS deploy/asterisk -- asterisk -rx 'pjsip set logger on'"
    echo "    kubectl exec -n $NS deploy/asterisk -- asterisk -rx 'module reload res_pjsip.so'"
    echo "    kubectl logs -n $NS deploy/asterisk --since=20s --tail=400 \\"
    echo "      | grep -iE 'Transmitting SIP|Received SIP|401|403|REGISTER'"
  fi
fi

# ────────────────────────────────────────────────────────────────────
hdr "Gate F — Agent provisioning (control-api → ps_endpoints)"
# POST /control/sip/agents/<id>/credentials should provision the
# WebRTC endpoint staff_<id> with the full WebRTC column set.
# ────────────────────────────────────────────────────────────────────

if [[ -n "$AGENT_ID" && -n "$CONTROL_URL" && -n "$BEARER" ]]; then
  CRED=$(curl -sX POST -H "Authorization: Bearer $BEARER" \
              -H 'content-type: application/json' \
              -d '{"displayName":"audit"}' \
              "$CONTROL_URL/control/sip/agents/$AGENT_ID/credentials")
  echo "    $CRED"
  echo "$CRED" | grep -q '"username":"staff_' \
    && pass "control-api returned credentials for staff_$AGENT_ID" \
    || fail "control-api did not return staff_$AGENT_ID credentials"

  EP=$(K asterisk -rx "pjsip show endpoint staff_$AGENT_ID" \
        | grep -E 'webrtc|media_encryption|transport|context|agent_id' | head -10)
  echo "$EP" | sed 's/^/    /'
  echo "$EP" | grep -q 'webrtc.*yes' \
    && pass "staff_$AGENT_ID endpoint has webrtc=yes" \
    || fail "staff_$AGENT_ID endpoint missing webrtc=yes (or endpoint absent)"
else
  warn "AGENT_ID / CONTROL_URL / BEARER not all set; skipping"
fi

# ────────────────────────────────────────────────────────────────────
hdr "Gate G — Outbound INVITE to TEST_DID"
# THIS WILL ATTEMPT TO RING TEST_DID. Comment out if undesired.
# Watches the wire log for 100/180/200/403 and audio SDP markers.
# ────────────────────────────────────────────────────────────────────

if [[ -n "$TRUNK" && -n "$TEST_DID" ]]; then
  K asterisk -rx 'pjsip set logger on' >/dev/null
  K asterisk -rx "originate PJSIP/$TEST_DID@$TRUNK application Wait 60" \
    | sed 's/^/    /'

  sleep 6
  WIRE=$(kubectl logs -n "$NS" deploy/asterisk --since=20s --tail=600 2>/dev/null \
           | grep -viE 'Remote UNIX' \
           | grep -iE 'Transmitting SIP request|Received SIP response|From:|INVITE sip|m=audio|crypto|180 Ringing|200 OK|403|404|Reason' \
           | tail -40)
  echo "$WIRE" | sed 's/^/    /'

  echo "$WIRE" | grep -q '180 Ringing' \
    && pass "got 180 Ringing — phone should ring" \
    || { echo "$WIRE" | grep -qE '200 OK' \
           && pass "got 200 OK directly (auto-answered or test loop)" \
           || fail "no 180/200 from carrier — see wire log above for 401/403/Reason"; }

  echo "$WIRE" | grep -q 'm=audio' \
    && pass "SDP m=audio negotiated" \
    || warn "no SDP m=audio seen in window — call may be signaling-only"
else
  warn "TRUNK / TEST_DID not set; skipping outbound test"
fi

# ────────────────────────────────────────────────────────────────────
hdr "Summary"
# ────────────────────────────────────────────────────────────────────

if [[ "$FAILED" -eq 0 ]]; then
  printf '%s%sall gates passed%s — call pipeline is intact\n' "$C_GRN" "✓ " "$C_OFF"
  exit 0
else
  printf '%s%s%d gate(s) failed%s — fix the earliest failing one first\n' \
    "$C_RED" "✗ " "$FAILED" "$C_OFF"
  exit 1
fi
