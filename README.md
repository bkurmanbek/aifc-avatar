# Avatar System Demo

This workspace is the flattened legacy/prototype avatar stack. The former
`realtime-avatar/` contents now live directly at this root.

## Layout

| Path | Role |
|---|---|
| `ws_backend/` | WebSocket backend prototype |
| `frontend/` | Local Vite frontend copy matching the production UI |
| `rag/` | Local RAG helpers for this prototype |
| `scripts/` | Smoke tests and local run helpers |
| `../data/` | Shared corpus, chunks, caches, vector DB, and voice refs |
| `.env` | Local prototype environment |

## Commands

```bash
cd /home/admin-aifc/avatar-system-2
bash scripts/run_ws_backend.sh
```

`scripts/run_ws_backend.sh` uses
`/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python` by default. Override
with `WS_BACKEND_PYTHON=/path/to/python` if a dedicated `.venv` is recreated.

Frontend commands:

```bash
cd /home/admin-aifc/avatar-system-2/frontend
npm install
npm run dev
```

Current local state includes `frontend/node_modules` and `frontend/dist` so
`npm run build` works without reinstalling dependencies first.

The UI source, public assets, and frontend config were copied from the
production frontend so the UX matches. Runtime code now uses this local copy;
local `frontend/.env*`, `frontend/.vercel`, `frontend/node_modules`, and
`frontend/dist` are kept as demo/deploy/runtime state.

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
