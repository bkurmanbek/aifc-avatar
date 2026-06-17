# DEPLOY.md — Avatar System 2 production deployment

How the demo is served to the public internet, and how to operate it.
_Last updated: 2026-06-17._

## Live URLs

| What | URL |
|---|---|
| Frontend (public) | https://frontend-five-lemon-98.vercel.app |
| Backend (public, via ngrok) | https://outdoor-yearlong-edythe.ngrok-free.dev |
| Vercel project | `bakytzhan-s-projects/frontend` |

## Architecture

```
            Browser
              │
              ├── HTML / JS / CSS / idle.mp4 / vendor assets ── Vercel (static SPA, CDN)
              │
              └── wss://…/ws  +  https://…/intro-video|intro-audio|intro-cache
                                   │  (ngrok static domain, TLS)
                                   ▼
                         H200 box (behind CGNAT):
                           ngrok agent  ──►  backend :8080 (FastAPI/uvicorn, CORS)
                                                   │ local HTTP
                                                   ▼
                                             SyncTalk :8005 (GPU inference)
```

Why this split: Vercel can only host the **static frontend**. The backend is a persistent
WebSocket + GPU pipeline and must run on the GPU box. The box is behind CGNAT (no public IP),
so it's exposed outbound via **ngrok** (cloudflared also works). The frontend reaches the
backend directly (WebSocket can't be proxied by Vercel), enabled by a single env knob +
backend CORS.

## Components & ports

| Service | Port | How it runs | Auto-start on boot |
|---|---|---|---|
| SyncTalk inference | 8005 | manual (`scripts/start_synctalk.sh`) or optional systemd template | **No** (see below) |
| Backend (uvicorn) | 8080 | `systemctl --user` → `avatar-backend.service` | **Yes** |
| ngrok tunnel | → 8080 | `systemctl --user` → `avatar-ngrok.service` | **Yes** |
| Frontend | — | Vercel (static) | n/a |

---

## Frontend on Vercel

- Repo: `bkurmanbek/aifc-avatar`, **root directory = `frontend`**, framework Vite.
- Build config lives in `frontend/vercel.json` (`npm ci` + `npm run build` → `dist`, SPA fallback).
- The CLI is logged in on the box as **bkurmanbek**.

### Project env vars (Production) — already set
| Var | Value | Purpose |
|---|---|---|
| `VITE_BACKEND_ORIGIN` | `https://outdoor-yearlong-edythe.ngrok-free.dev` | Backend origin → drives WS URL (https→wss) **and** all backend HTTP asset URLs |
| `VITE_AVATAR_LABEL` | `aifc-avatar-5-3min_exp_6` | Avatar label overlay |
| `VITE_IDLE_VIDEO_SRC` | `/idle.mp4` | Idle clip (served by Vercel from `frontend/public`) |

`VITE_BACKEND_ORIGIN` is the single knob: empty → same-origin/local; set → split deploy.
See `frontend/src/utils.ts` (`BACKEND_ORIGIN`, `backendHttpUrl`, `backendWsUrl`).

### Redeploy the frontend
```bash
cd frontend
vercel deploy --prod         # uses the persisted project env vars
```
(Env vars are stored on the project, so no `-b` flags are needed anymore. If you ever change
the backend origin, update it: `vercel env rm VITE_BACKEND_ORIGIN production` then
`printf "<new-origin>" | vercel env add VITE_BACKEND_ORIGIN production`, and redeploy.)

---

## Backend exposure (ngrok)

- Reserved **static** domain `outdoor-yearlong-edythe.ngrok-free.dev` (ngrok free tier, 1 domain).
  Because it's static, the Vercel build's baked-in origin keeps working across restarts/reboots.
- Authtoken is in `~/.config/ngrok/ngrok.yml`.
- ngrok-free serves a browser interstitial; the frontend sends `ngrok-skip-browser-warning: true`
  on its asset `fetch()`s to bypass it (see `useChunkPlayback.ts`). The intro `<video>` can't send
  headers — if the intro ever fails to load on ngrok-free, switch it to a header-bearing blob fetch
  (or move to a paid ngrok / custom domain, which removes the interstitial entirely).

---

## systemd (user) services

These are **user** services (no root needed). **Linger is enabled** (`loginctl enable-linger
admin-aifc`) so they start at boot without an interactive login. Unit sources live in the repo
under `deploy/systemd/`; installed copies are in `~/.config/systemd/user/`.

All `systemctl --user` commands need the runtime dir exported when run from a non-login shell:
```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
```

### Install / update from the repo copies
```bash
cp deploy/systemd/avatar-backend.service deploy/systemd/avatar-ngrok.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now avatar-backend.service avatar-ngrok.service
```

### Operate
```bash
systemctl --user status  avatar-backend.service avatar-ngrok.service
systemctl --user restart avatar-backend.service      # e.g. after a config.env / code change
systemctl --user stop    avatar-ngrok.service
journalctl --user -u avatar-backend.service -f       # live logs (replaces var/backend.log)
journalctl --user -u avatar-ngrok.service   -n 50
```

### Gotcha baked into the unit: `LD_LIBRARY_PATH`
The backend runs the **base-conda** Python (`WS_BACKEND_PYTHON` in `.env`). Under systemd's clean
env the system `libstdc++` loads first and the import chain dies with
`CXXABI_1.3.15 not found`. The unit sets `Environment=LD_LIBRARY_PATH=/home/admin-aifc/miniforge3/lib`
to force conda's libstdc++. Keep that line if you edit the unit.

### SyncTalk (intentionally NOT auto-managed)
SyncTalk is the heavy GPU service; we avoid disturbing a running instance. A reviewed template is at
`deploy/systemd/avatar-synctalk.service`. To make SyncTalk survive reboots too:
```bash
# only when no SyncTalk is currently running, or you're OK restarting it:
cp deploy/systemd/avatar-synctalk.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now avatar-synctalk.service   # ~60-100s to load model + frames
```
Until then, after a reboot start it manually: `bash scripts/start_synctalk.sh`.

---

## Reboot behavior

- **Backend + ngrok auto-start** (enabled + linger). The ngrok domain is static, so the live
  Vercel site keeps working with **no redeploy**.
- **SyncTalk does NOT auto-start** unless you enable its unit (above). After a reboot the avatar
  won't render until SyncTalk is up — either enable the unit or run `scripts/start_synctalk.sh`.
- Full bring-up after a cold reboot (if SyncTalk unit not enabled):
  ```bash
  bash scripts/start_synctalk.sh                       # GPU service
  export XDG_RUNTIME_DIR=/run/user/$(id -u)
  systemctl --user status avatar-backend avatar-ngrok  # these came up on their own
  ```

---

## STT — Soniox v5

`config.env` uses **`SONIOX_STT_MODEL=stt-rt-v5`** (v4 retires 2026-06-30; v5 is a drop-in with
the same params). v5 improves noisy/far-field/multi-speaker accuracy, accented + multilingual
speech, and alphanumeric precision (IINs, codes, emails) — all relevant to AIFC queries.

### Tuning knobs relevant to this conversational use-case
| Setting (config.env) | What it does | Recommendation |
|---|---|---|
| `SONIOX_STT_MODEL` | model id | `stt-rt-v5` |
| `SONIOX_STT_ENDPOINT_SENSITIVITY` | **v5** endpointing: higher = finalize sooner (snappier), lower = wait longer (fewer mid-sentence cutoffs). Empty = model default. Sent only when set (`backend/media/stt.py`). | Start empty; if turns feel slow, nudge up; if it cuts people off, nudge down. |
| `SONIOX_STT_LANGUAGE_HINTS` + `_STRICT` | bias to en/ru/kk/zh | keep as-is |
| `SONIOX_STT_MAX_ENDPOINT_DELAY_MS` / `_ENDPOINT_WAIT_S` | legacy endpoint timing | keep; complements sensitivity |
| context terms (`backend/media/stt.py SONIOX_STT_CONTEXT`) | AIFC vocabulary biasing | already populated |

Features intentionally **off** for a single-user kiosk: speaker diarization and real-time
translation (we transcribe in the source language and let Gemini handle language). Enable only if
the use-case changes.

Confirm v5 is live after a query: `journalctl --user -u avatar-backend -g stt_config_send` should
show `model=stt-rt-v5`.

---

## Caveats / upgrade path

- **ngrok-free limits**: bandwidth/rate caps + the interstitial. Frame streaming is heavy; under
  real load consider a paid ngrok tier or a ~$10/yr domain + a named **Cloudflare tunnel**
  (`cloudflared`, already installed) — both remove the interstitial and the limits.
- An **ephemeral Cloudflare quick tunnel** may still be running from earlier testing
  (`var/log/cloudflared.log`); it's not part of this deployment and can be killed
  (`pkill -f "cloudflared tunnel"`).
- CORS allowlist defaults to `*` (`CORS_ALLOW_ORIGINS` in the backend env). Tighten to the Vercel
  origin for production hardening.
