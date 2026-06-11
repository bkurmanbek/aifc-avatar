# Avatar System Demo

This workspace contains the compact realtime conversational avatar stack.

Primary flow:

```text
User speech -> STT -> frontend transcript -> LLM streaming -> sentence TTS -> SyncTalk avatar stream
```

## Layout

| Path | Role |
|---|---|
| `backend/` | Canonical backend package and live realtime pipeline |
| `ws_backend/` | Thin compatibility shim for old entrypoints |
| `frontend/` | Local Vite frontend copy matching the production UI |
| `rag/` | Local RAG helpers for this prototype |
| `scripts/` | Single-stack launcher, stop script, and smoke tests |
| `logs/` | Reset-on-restart module logs |
| `archive/` | Old generated/runtime/scratch artifacts moved out of the root |
| `docs/` | Architecture, logging, and protocol notes |
| `../data/` | Shared corpus, chunks, caches, vector DB, and voice refs |
| `.env` | Local prototype environment |

## Commands

Single-avatar realtime stack (SyncTalk + backend + frontend):

```bash
cd /home/admin-aifc/avatar-system-2
bash scripts/run_single_avatar.sh
```

The demo uses:

- SyncTalk avatar: `aifc-avatar-5-exp-5-v3`
- TTS provider: Soniox realtime TTS (`TTS_PROVIDER=soniox`, voice `SONIOX_TTS_VOICE`)
- SyncTalk port: `8005`
- Backend port: `8080`
- Frontend port: `5173`
- Idle video: `./frontend/public/idle.mp4`

Stop the stack:

```bash
cd /home/admin-aifc/avatar-system-2
bash scripts/stop_single_avatar.sh
```

Backend only:

```bash
cd /home/admin-aifc/avatar-system-2
bash scripts/run_ws_backend.sh
```

`scripts/run_ws_backend.sh` uses
`/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python` by default. Override
with `WS_BACKEND_PYTHON=/path/to/python` if a dedicated `.venv` is recreated.
The canonical backend app is `backend.app.main:app`.

Frontend only:

```bash
cd /home/admin-aifc/avatar-system-2/frontend
npm install
npm run dev
```

## Smoke Tests

```bash
cd /home/admin-aifc/avatar-system-2
python scripts/smoke_ws_text.py
python scripts/smoke_ws_interrupt.py
python scripts/capture_ws_tts.py --text Hello --out rec_1.wav
```

See also:

- `docs/ARCHITECTURE.md`
- `docs/WEBSOCKET_PROTOCOL.md`
- `docs/LOGGING.md`

The UI source, public assets, and frontend config are local to this workspace.
Generated frontend output, runtime logs, recordings, pid files, and old demo
experiments are ignored or moved to `archive/`.

## Canonical Production Stack

The separate production stack is:

```text
/home/admin-aifc/avatar_system
```

The clean operator hub is:

```text
/home/admin-aifc/runtime
```

This demo should not import backend Python modules from that production folder.
Use `/home/admin-aifc/runtime/bin/aifc-stack status` only for production service
status, not for this demo runtime.
