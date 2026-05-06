# Call-engine config snippets

These sample fragments wire Asterisk to the
[`agenthub-call-engine`](https://github.com/Velents-Technologies-UG/agent-hub/tree/claude/build-call-engine-service-pxwcB/services/agenthub-call-engine)
service that lives in the `agent-hub` repo.

## Files

- `extensions_ai_runtime.conf.sample` - the `[ai-runtime]` context that runs
  `AudioSocket()` plus a `[call-engine-test]` context for ext `100`.
- `ari_call_engine.conf.sample` - the ARI user and HTTP server settings the
  call-engine connects to.

## Install

From the host running Asterisk:

```bash
sudo cp extensions_ai_runtime.conf.sample /etc/asterisk/extensions_ai_runtime.conf
# then in /etc/asterisk/extensions.conf add:
#   #include extensions_ai_runtime.conf

# merge the snippets into /etc/asterisk/ari.conf and /etc/asterisk/http.conf
# (don't overwrite existing users you rely on).

sudo asterisk -rx 'dialplan reload'
sudo asterisk -rx 'module reload res_ari.so'
sudo asterisk -rx 'module reload res_http_websocket.so'
```

Verify the modules required by the call-engine are loaded:

```
asterisk -rx 'module show like audiosocket'
asterisk -rx 'module show like stasis'
asterisk -rx 'module show like ari'
```

## Smoke test

1. Start `agenthub-call-engine` (`npm start` in `services/agenthub-call-engine`).
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
