# LiveAvatar — LITE mode (modular), Google Sulafat TTS

Standalone, Vercel-deployable demo of HeyGen **LiveAvatar LITE mode (modular integration)**: we
bring our own TTS (**Google Gemini, voice "Sulafat"**) and LiveAvatar only renders the lip-synced
avatar video. Does **not** touch the SyncTalk pipeline (`backend/`, `frontend/`).

```
text ──/api/tts──> Gemini Sulafat (PCM 16-bit 24kHz)
                         │  base64, 600ms+1s chunks
                         ▼
browser ── WebSocket(ws_url) ──>  LiveAvatar  ── LiveKit room ──>  <video> (lip-synced avatar)
   ▲ agent.speak / agent.speak_end / agent.interrupt / session.keep_alive
   │
   └─ /api/session (server, X-API-KEY) → session_token → start → { livekit_url, livekit_client_token, ws_url }
```

LITE = 1 credit/min. **PCM 16-bit / 24 kHz / mono** is mandatory — Gemini Sulafat outputs exactly
that, so **no resampling**. (Ref: HeyGen `liveavatar-integrate` skill → `lite-mode-guide.md`.)

## What you must provide (env vars on the new Vercel project + local `.env`)

| Var | Where | Notes |
|---|---|---|
| `LIVEAVATAR_API_KEY` | https://app.liveavatar.com | **Server-side only.** |
| `LIVEAVATAR_AVATAR_ID` | LiveAvatar dashboard or `GET /v1/avatars` | ⚠️ must be a **LiveAvatar-enabled** avatar — see below. |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | Google AI Studio | for the Sulafat TTS (`/api/tts`). |
| `GEMINI_TTS_VOICE` | optional, default `Sulafat` | |
| `GEMINI_TTS_MODEL` | optional, default `gemini-3.1-flash-tts-preview` | |
| `LIVEAVATAR_SANDBOX` | optional, `1` to use a sandbox avatar (no credits) | overrides avatar_id with the sandbox id |

## ⚠️ The avatar-ID caveat (likely first blocker)

Your avatar `5370f4fc3bef4fe7bb2934be58cf718e` is a **personal HeyGen custom avatar, not yet on
LiveAvatar.** LiveAvatar only accepts avatars listed by `GET /v1/avatars` (its own enabled set). So
before this works you must **import/enable that avatar in your LiveAvatar account** (app.liveavatar.com)
and use the id it shows there. To prove the pipeline first, set `LIVEAVATAR_SANDBOX=1` (uses HeyGen's
sandbox avatar `dd73ea75-1218-4ef3-92ce-606d5f7fbc0a`, ~1-min free sessions, no credits).

## Run / deploy

```bash
cd HeyGen
npm install
cp .env.example .env     # fill LIVEAVATAR_API_KEY, LIVEAVATAR_AVATAR_ID, GEMINI_API_KEY
npx vercel dev           # local: serves the page + /api/session + /api/tts
# ── new Vercel project (separate URL) ──
vercel link              # create a NEW project
vercel env add LIVEAVATAR_API_KEY     # + LIVEAVATAR_AVATAR_ID, GEMINI_API_KEY (+ optional ones)
vercel deploy --prod
```

## How it works (`src/main.ts`)

1. `POST /api/session` → server mints the LITE token (`X-API-KEY`) + starts the session, returns
   `livekit_url`, `livekit_client_token`, `ws_url`.
2. Frontend joins the **LiveKit** room (`livekit-client`) and attaches the avatar's video (+audio)
   track to the `<video>`.
3. Frontend opens a **WebSocket** to `ws_url`, waits for `session.state_updated: connected`, then
   keeps it alive (`session.keep_alive` every 2 min).
4. Type text → `POST /api/tts` → Gemini Sulafat returns base64 **PCM 24 kHz** → the browser splits it
   into 600 ms + 1 s chunks and sends `agent.speak` (same `event_id`) then `agent.speak_end`.
5. Stop = `agent.interrupt` + halt the send loop. Teardown on unload = `sendBeacon('/api/stop')` →
   server `POST /v1/sessions/stop` (Bearer) so the session stops immediately instead of lingering to
   the 5-min idle timeout. (The guide's `DELETE /v1/sessions` returns 405 — `POST .../stop` is correct.)

## Notes / assumptions to verify with your key

- Built strictly to HeyGen's `lite-mode-guide.md` protocol. The frontend uses raw `livekit-client`
  (not `@heygen/liveavatar-web-sdk`) so the `/start` call + creds are explicit and unambiguous; the
  official SDK is an alternative (`https://github.com/heygen-com/liveavatar-web-sdk`, `apps/demo`).
- Assumes LiveAvatar publishes the (re-timed) audio track into the room so A/V stays in sync; if it
  doesn't, play the TTS audio locally against the video clock instead.
- Untested end-to-end here (no LiveAvatar key on this box). It builds + deploys; full function needs
  the key + a LiveAvatar-enabled avatar (or sandbox).
