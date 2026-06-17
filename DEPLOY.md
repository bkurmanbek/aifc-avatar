# DEPLOY.md — Avatar System 2 production deployment

How the demo is served to the public internet, and how to operate it.
_Last updated: 2026-06-17 (backend transport migrated ngrok → Cloudflare Tunnel)._

## Live URLs

| What | URL |
|---|---|
| Frontend (public) | https://frontend-five-lemon-98.vercel.app |
| Backend (public, via Cloudflare Tunnel) | https://avatar.bk-project.org |
| Vercel project | `bakytzhan-s-projects/frontend` |

> **Transport note:** the backend was originally exposed via **ngrok-free**, but that tunnel
> couldn't sustain the ~10 Mbps binary avatar-frame stream — frames backed up (client render lag
> grew to ~5s) and playback stuttered. We migrated to a **named Cloudflare Tunnel**
> (`avatar.bk-project.org`), which has no bandwidth throttle (box uplink ~88 Mbps is the ceiling)
> and a PoP in Almaty (`ala02`). Render lag dropped from ~4.8s to ~2.2s (the designed prebuffer
> lead) at full frame quality. ngrok is retired (service disabled, see below).

## Architecture

```
            Browser
              │
              ├── HTML / JS / CSS / idle.mp4 / vendor assets ── Vercel (static SPA, CDN)
              │
              └── wss://…/ws  +  https://…/intro-video|intro-audio|intro-cache
                                   │  (Cloudflare Tunnel, TLS at edge)
                                   ▼
                         H200 box (behind CGNAT):
                           cloudflared  ──►  backend :8080 (FastAPI/uvicorn, CORS)
                                                   │ local HTTP
                                                   ▼
                                             SyncTalk :8005 (GPU inference)
```

Why this split: Vercel can only host the **static frontend**. The backend is a persistent
WebSocket + GPU pipeline and must run on the GPU box. The box is behind CGNAT (no public IP),
so it's exposed outbound via a **named Cloudflare Tunnel** (ngrok was the original choice but
throttled the frame stream — see transport note above). The frontend reaches the backend
directly (WebSocket can't be proxied by Vercel), enabled by a single env knob + backend CORS.

## Components & ports

| Service | Port | How it runs | Auto-start on boot |
|---|---|---|---|
| SyncTalk inference | 8005 | manual (`scripts/start_synctalk.sh`) or optional systemd template | **No** (see below) |
| Backend (uvicorn) | 8080 | `systemctl --user` → `avatar-backend.service` | **Yes** |
| Cloudflare Tunnel | → 8080 | `systemctl --user` → `avatar-cloudflared.service` | **Yes** |
| ~~ngrok tunnel~~ | — | `avatar-ngrok.service` — **disabled/retired** (kept as fallback unit only) | No |
| Frontend | — | Vercel (static) | n/a |

---

## Frontend on Vercel

- Repo: `bkurmanbek/aifc-avatar`, **root directory = `frontend`**, framework Vite.
- Build config lives in `frontend/vercel.json` (`npm ci` + `npm run build` → `dist`, SPA fallback).
- The CLI is logged in on the box as **bkurmanbek**.

### Project env vars (Production) — already set
| Var | Value | Purpose |
|---|---|---|
| `VITE_BACKEND_ORIGIN` | `https://avatar.bk-project.org` | Backend origin → drives WS URL (https→wss) **and** all backend HTTP asset URLs |
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

## Backend exposure (Cloudflare Tunnel)

Named tunnel **`avatar`** (id `45795a00-dd97-45d3-a827-7cf4b43013a8`) on domain `bk-project.org`
(zone managed by Cloudflare). Hostname `avatar.bk-project.org` → `http://localhost:8080`.

- Config + credentials live in `~/.cloudflared/`:
  - `config.yml` — tunnel id, credentials path, ingress (`avatar.bk-project.org` → localhost:8080).
  - `<tunnel-id>.json` — tunnel credentials (**secret**, do not commit).
  - `cert.pem` — origin cert from `cloudflared tunnel login` (**secret**).
- TLS terminates at the Cloudflare edge (Universal SSL, auto-renewed). After first creating the
  hostname, the edge cert takes ~minutes to provision (DNS resolves first, HTTPS fails with a TLS
  handshake error until the cert is live — this is normal for a new zone).
- No interstitial and no bandwidth throttle (unlike ngrok-free). The frontend still sends
  `ngrok-skip-browser-warning: true` on its asset fetches and loads the intro MP4 via a
  header-bearing **blob fetch** (`useChunkPlayback.ts` / `App.tsx`) — harmless on Cloudflare and
  keeps it portable if the transport ever changes again.

### Recreate the tunnel from scratch (if ever needed)
```bash
cloudflared tunnel login                                   # browser auth → ~/.cloudflared/cert.pem
cloudflared tunnel create avatar                           # → <id>.json credentials
cloudflared tunnel route dns avatar avatar.bk-project.org  # CNAME in Cloudflare
# write ~/.cloudflared/config.yml (tunnel id, credentials-file, ingress → localhost:8080)
cloudflared tunnel ingress validate
```

### ngrok (retired)
The old `avatar-ngrok.service` (static domain `outdoor-yearlong-edythe.ngrok-free.dev`, authtoken in
`~/.config/ngrok/ngrok.yml`) is **disabled and stopped**. The unit file is kept under
`deploy/systemd/` as a fallback only. To fail back temporarily: set `VITE_BACKEND_ORIGIN` back to the
ngrok domain, redeploy, and `systemctl --user enable --now avatar-ngrok.service`.

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
cp deploy/systemd/avatar-backend.service deploy/systemd/avatar-cloudflared.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now avatar-backend.service avatar-cloudflared.service
```

### Operate
```bash
systemctl --user status  avatar-backend.service avatar-cloudflared.service
systemctl --user restart avatar-backend.service          # e.g. after a config.env / code change
systemctl --user restart avatar-cloudflared.service      # e.g. after editing ~/.cloudflared/config.yml
journalctl --user -u avatar-backend.service -f           # live logs (replaces var/backend.log)
journalctl --user -u avatar-cloudflared.service -n 50    # tunnel connections / errors
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

- **Backend + Cloudflare Tunnel auto-start** (enabled + linger). The hostname is fixed
  (`avatar.bk-project.org`), so the live Vercel site keeps working with **no redeploy**.
- **SyncTalk does NOT auto-start** unless you enable its unit (above). After a reboot the avatar
  won't render until SyncTalk is up — either enable the unit or run `scripts/start_synctalk.sh`.
- Full bring-up after a cold reboot (if SyncTalk unit not enabled):
  ```bash
  bash scripts/start_synctalk.sh                              # GPU service
  export XDG_RUNTIME_DIR=/run/user/$(id -u)
  systemctl --user status avatar-backend avatar-cloudflared   # these came up on their own
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

- **Why we left ngrok-free:** it couldn't sustain the ~10 Mbps binary avatar-frame stream
  (each frame ≈ 50 KB JPEG × 25 fps); frames backed up in the tunnel and playback stuttered.
  The box uplink is ~88 Mbps, so the box was never the bottleneck — the free tunnel was.
  Cloudflare Tunnel has no such throttle.
- **Cloudflare free-plan TOS (§2.8)** discourages serving large amounts of *cached* video/media via
  the CDN. Our traffic is a **WebSocket application stream** (not cached media files) on a
  low-traffic demo, which is fine. If usage grows a lot, a paid Cloudflare plan removes any doubt.
- If you ever see a stray **ephemeral quick tunnel** (`cloudflared tunnel --url ...`) from manual
  testing, it's unrelated to this deployment — kill it by PID (avoid `pkill -f cloudflared`, which
  would also hit the real `avatar` tunnel).
- CORS allowlist defaults to `*` (`CORS_ALLOW_ORIGINS` in the backend env). Tighten to the Vercel
  origin for production hardening.
