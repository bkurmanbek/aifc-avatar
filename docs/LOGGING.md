# Logging

Root module logs are written to `logs/`:

- `logs/backend.log`
- `logs/frontend.log`
- `logs/stt.log`
- `logs/llm.log`
- `logs/tts.log`
- `logs/avatar.log`
- `logs/pipeline.log`
- `logs/websocket.log`
- `logs/errors.log`

Backend startup resets `logs/` by default through `backend.app.main`. Set `RESET_LOGS_ON_START=false` when a parent launcher already reset logs for a multi-process stack.

Frontend `npm run dev` runs `frontend/scripts/dev-with-logs.mjs`, which resets `logs/` by default and tees Vite output to `logs/frontend.log`. Browser runtime errors are sent to the backend as `client_log` messages when the WebSocket is open.

Process logs from stack scripts still live in `var/log/*` during a run. Root `logs/*.log` are module logs; `var/log/*` are process stdout/stderr logs.

Useful commands:

```bash
tail -f logs/pipeline.log logs/tts.log logs/avatar.log logs/errors.log
tail -f logs/websocket.log logs/frontend.log
```

Correlation fields:

- `session_id`: WebSocket session.
- `request_id`: response `turn_id`.
- `event`: stage transition or status.
- `latency_ms`: stage duration where available.

