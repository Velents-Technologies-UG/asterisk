# Call-engine config snippets

These sample fragments wire Asterisk to the **`call-engine`** service, which
now lives in its own standalone repo (sibling checkout `../call-engine`). It
was extracted from agent-hub's never-merged `claude/build-call-engine-service-pxwcB`
branch — that branch and the old `agent-hub/services/agenthub-call-engine`
path are both dead; do not wire against them.

## Files

`.sample` = copy/merge by hand. `.template` = rendered at container start by
`deploy/entrypoint.sh` via `envsubst`, and is the path the deployed image
actually uses.

Call engine (Stasis app + AudioSocket bridge):

- `extensions_ai_runtime.conf.sample` - the `[ai-runtime]` context that runs
  `AudioSocket()`, plus `[call-engine-test]` (ext `100`), `[from-flows]`,
  `[from-trunk]`, `[from-agents]`, `[from-wss-agents-out]`, `[from-trunk-out]`.
- `ari.conf.template` - the ARI user (`[asterisk]`, `${ARI_PASSWORD}`) plus the
  `http.conf` settings ARI needs, commented at the bottom. This is the file the
  container renders; `ari.conf.sample` next to it is **stock upstream
  reference**, not ours. ARI is served on **8088**, together with the softphone
  WebSocket upgrade — one HTTP server, one port, not separable (measured
  2026-08-13; `8092` is the control-api sidecar and 404s every `/ari/` path).

Browser softphone for human agents (PJSIP WSS + WebRTC + realtime):

- `http_wss.conf.sample` - enables `tlsbindaddr=0.0.0.0:8089` so browsers
  can `wss://` into Asterisk.
- `pjsip_wss_agents.conf.sample` - `[transport-wss]` plus the
  `agent-endpoint-template`, `agent-aor-template`, `agent-auth-template`
  used by the per-agent rows the API provisions, and a static `[agent-demo]`
  for connectivity testing.
- `sorcery_realtime_agents.conf.sample` - points res_pjsip at the realtime
  backend.
- `extconfig_realtime_agents.conf.sample` - maps `ps_endpoints` /
  `ps_auths` / `ps_aors` to the `asterisk` ODBC connection.
- `res_odbc_agents.conf.sample` - the ODBC connection (Postgres via
  unixodbc + odbc-postgresql). `res_odbc_agents.conf.template` is the
  env-substituted variant the container renders; `deploy/render_odbc.py`
  writes the matching `odbcinst.ini` / `odbc.ini`.
- `pjsip_transport_tls.conf.template` - `[transport-tls]` for SIP-over-TLS
  trunks, rendered alongside the WSS transport.

## Install (call engine)

```bash
sudo cp extensions_ai_runtime.conf.sample /etc/asterisk/extensions_ai_runtime.conf
# in /etc/asterisk/extensions.conf add:
#   #include extensions_ai_runtime.conf

# ari.conf: the container renders ari.conf.template for you (needs ARI_PASSWORD
# in the environment). For a hand-built host, substitute it yourself:
#   ARI_PASSWORD=... envsubst < ari.conf.template > /etc/asterisk/ari.conf
# then merge that file's trailing commented [general] block into http.conf
# (enabled=yes, bindaddr=0.0.0.0, bindport=8088).

sudo asterisk -rx 'dialplan reload'
sudo asterisk -rx 'module reload res_ari.so'
sudo asterisk -rx 'module reload res_http_websocket.so'
```

## Install (browser softphone)

```bash
# 1. TLS cert for WSS (self-signed for local testing)
sudo mkdir -p /etc/asterisk/keys
cd /etc/asterisk/keys
sudo openssl req -x509 -newkey rsa:2048 -keyout asterisk.key -out asterisk.pem \
  -days 365 -nodes -subj "/CN=asterisk.local"
sudo chown asterisk:asterisk asterisk.{key,pem}
sudo chmod 640 asterisk.{key,pem}

# 2. Merge http_wss.conf.sample into /etc/asterisk/http.conf
# 3. Append pjsip_wss_agents.conf.sample to /etc/asterisk/pjsip.conf
#    (or #include it)

# 4. ODBC -> Postgres realtime
sudo apt install -y unixodbc odbc-postgresql
# edit /etc/odbcinst.ini and /etc/odbc.ini per res_odbc_agents.conf.sample
sudo cp res_odbc_agents.conf.sample /etc/asterisk/res_odbc.conf
sudo cp sorcery_realtime_agents.conf.sample /etc/asterisk/sorcery.conf
# merge extconfig_realtime_agents.conf.sample into /etc/asterisk/extconfig.conf

# 5. Apply the PJSIP realtime schema (call-engine repo)
cd /path/to/call-engine
npm run migrate    # migrations/0002_pjsip_realtime.sql onward:
                   # ps_endpoints, ps_auths, ps_aors, ps_identify,
                   # ps_endpoints.outbound_proxy (0008)

# 6. Reload Asterisk
sudo asterisk -rx 'module reload res_odbc.so'
sudo asterisk -rx 'module reload res_sorcery_realtime.so'
sudo asterisk -rx 'module reload res_pjsip.so'
sudo asterisk -rx 'pjsip show transports'      # expect transport-wss
sudo asterisk -rx 'odbc show all'              # expect Connected: yes
```

Verify the modules required are loaded:

```
asterisk -rx 'module show like audiosocket'
asterisk -rx 'module show like stasis'
asterisk -rx 'module show like ari'
asterisk -rx 'module show like pjsip_transport_websocket'
asterisk -rx 'module show like sorcery_realtime'
asterisk -rx 'module show like config_odbc'
```

## Smoke test (call engine)

1. Start the call-engine (`npm start` in the `call-engine` repo).
2. The service log should show:
   `AudioSocket stub listening on 0.0.0.0:8090` and
   `ARI Stasis app 'call-engine' subscribed and ready`.
3. Dial extension `100` from a registered SIP device.
4. Expected log lines on the call-engine side:
   - `StasisStart customer=...`
   - `Bridge ... created; customer ... added`
   - `Originated Local/s@ai-runtime ...`
   - `AudioSocket connection from ...`
   - `AudioSocket uuid=... from ...`
   - `Starting playback for uuid=...`
   - `AudioSocket session ended ... reason=playback_complete`
5. The caller hears a 3-second 440 Hz tone (the default sample WAV) and the
   call hangs up cleanly.

## Smoke test (browser softphone)

1. With Asterisk running and PJSIP realtime configured:
   `asterisk -rx 'pjsip show endpoints'` on a clean DB shows only `agent-demo`.
2. From AgentHub log in as a user with role `agent`, the `<Softphone>` mounts
   and `POST /api/agents/<your-id>/sip-credentials` is called automatically.
   That proxies to `POST /control/sip/agents/<id>/credentials` on the
   **control-api sidecar in the Asterisk pod** (`deploy/control_api.py`), which
   is what performs the `ps_auths` / `ps_aors` / `ps_endpoints` writes — see
   `deploy/README.md`.
3. `psql -c "SELECT id, username FROM ps_auths"` shows `agent_<your-id>`.
4. `asterisk -rx 'pjsip show contacts'` shows the agent's contact registered
   over `ws` after a few seconds.
5. Dial the agent's extension (`agent_<your-id>`) from another softphone or
   the AI runtime - the browser tab plays remote audio and the mic indicator
   lights up.
