# STT Endpointing & Turn Finalization

How the avatar decides "the user has stopped talking" and what to tune when it
fires **too early** (cuts users off mid-sentence). Grounded in the actual code:
`backend/media/stt.py`, `backend/settings.py`, `backend/session/session.py`,
`frontend/src/activeListeningConfig.ts`, `backend/utils/language.py`.

## TL;DR

There are **two endpointers racing**:

1. **Client-side VAD** (Silero in the browser) — after `redemptionMs` (800 ms) of
   silence it fires `onSpeechEnd` → sends `{type:'audio_end'}` to the backend.
2. **Server-side Soniox v5 endpoint** — Soniox emits an `<end>` token when it
   decides the utterance is over, governed by `max_endpoint_delay_ms` and
   `endpoint_sensitivity`.

Whichever fires first ends the turn. `SONIOX_STT_ENDPOINT_WAIT_S` is **not** the
primary endpoint timer — it is a backend grace window that runs *after* the
client VAD's `audio_end`, waiting for an already-pending Soniox `<end>` before
force-finalizing. To genuinely stop early-firing you tune all three together.

---

## The three knobs and exactly what each controls

### 1. `max_endpoint_delay_ms` (server-side Soniox) — config.env `SONIOX_STT_MAX_ENDPOINT_DELAY_MS=800`

Sent verbatim to Soniox in the session config (`backend/media/stt.py` `_soniox_config`):

```python
"enable_endpoint_detection": SONIOX_STT_ENABLE_ENDPOINT_DETECTION,  # True
"max_endpoint_delay_ms":     SONIOX_STT_MAX_ENDPOINT_DELAY_MS,      # 800
```

This is the **maximum** time after detected speech-stop that Soniox will wait
before forcing an endpoint (emitting `<end>`). It is an upper bound, not a fixed
pause. With `endpoint_sensitivity` unset, the model's own acoustic/linguistic
endpoint judgement fires *somewhere up to* this delay. Larger → Soniox tolerates
longer pauses before declaring the turn over. This is the real server-side
"don't cut me off" lever.

### 2. `endpoint_sensitivity` (server-side Soniox v5) — config.env `SONIOX_STT_ENDPOINT_SENSITIVITY=` (unset)

Only sent when explicitly set (so default/v4 behaviour is unchanged):

```python
if SONIOX_STT_ENDPOINT_SENSITIVITY is not None:
    config["endpoint_sensitivity"] = float(SONIOX_STT_ENDPOINT_SENSITIVITY)
```

Per the `settings.py` comment (the authoritative direction):

> higher = finalize sooner (snappier), lower = wait longer (fewer mid-sentence cutoffs)

So **lower `endpoint_sensitivity` reduces early-firing.** This is the most direct
v5 lever for the exact symptom we have (users cut off mid-sentence): it makes the
model more conservative about declaring an utterance finished, independent of the
hard `max_endpoint_delay_ms` ceiling.

### 3. `SONIOX_STT_ENDPOINT_WAIT_S` (backend grace window) — config.env, now `1.5`

This is **purely backend-side** and only matters on the *client-VAD* finalization
path. In `backend/session/session.py` `handle_audio_end()`:

```python
text, language = await active_session.wait_committed(SONIOX_STT_ENDPOINT_WAIT_S)
if not text:
    await active_session.send_silence(300)
    text, language = await active_session.finalize(close_after=False)
```

Sequence when the client VAD says "speech ended":
1. Wait up to `SONIOX_STT_ENDPOINT_WAIT_S` for Soniox to have *already* committed
   an `<end>` (the `_commit_event`). If Soniox is still holding the utterance open
   across a natural pause, no commit arrives yet.
2. If still no text, **force-finalize**: push 300 ms of silence + send a `finalize`
   message, then wait `SONIOX_STT_REALTIME_FINALIZE_TIMEOUT_S` (1.0 s) for the
   committed text.

So `ENDPOINT_WAIT_S` controls *how patient the backend is for a natural Soniox
endpoint before it forces one*. Raising 0.4 → 1.5 means: if the user pauses and
the client VAD prematurely fires `audio_end`, the backend now waits 1.5 s for the
user to resume (and for Soniox to deliver a natural `<end>`) before it cuts in
with a forced finalize. It does **not** affect the path where Soniox itself
endpoints first (`on_realtime_final` → `process_final_transcript`); that path is
governed by knobs #1 and #2.

### Related: the finalize timeouts (not endpoint timers)

- `SONIOX_STT_FINALIZE_TIMEOUT_S` (1.0) / `SONIOX_STT_REALTIME_FINALIZE_TIMEOUT_S`
  (1.0) — how long `finalize()`/`wait_committed()` waits for Soniox to flush the
  committed transcript *after* a `finalize` message is sent. This is a network/
  flush timeout, **not** an endpoint-detection delay. Leave as-is; raising it only
  adds latency when Soniox is slow to flush, it does not prevent early cutoffs.

---

## Why the config change is `SONIOX_STT_ENDPOINT_WAIT_S` (and what it is *not*)

The change made: `SONIOX_STT_ENDPOINT_WAIT_S` `0.4 -> 1.5`.

- It is the correct **backend** lever for "wait longer before finalizing the turn":
  it directly delays the backend force-finalize after a client-VAD `audio_end`,
  giving a paused user time to resume.
- It is **not** the whole story. The dominant early-firing causes are the
  client VAD (`redemptionMs`, 800 ms — usually fires first) and the server-side
  Soniox endpoint (`max_endpoint_delay_ms` + `endpoint_sensitivity`). If those
  fire and produce a committed final first, raising `ENDPOINT_WAIT_S` has no
  effect on that turn. For a complete fix, tune those too (recommendation below).

---

## Recommended tuning strategy (stop early-firing without laggy turn-end)

The two endpointers must be balanced: make the **client VAD** the coarse,
forgiving gate, and let **Soniox** make the precise call, while the **backend
grace window** absorbs the seam.

Recommended target values:

| Knob | Where | Current | Recommended | Effect |
|---|---|---|---|---|
| `redemptionMs` | client VAD (`activeListeningConfig.ts`, `VITE_ACTIVE_FINAL_SILENCE_MS`) | 800 | 900–1100 | Client VAD waits longer in silence before sending `audio_end`; biggest single lever since it usually fires first. |
| `SONIOX_STT_MAX_ENDPOINT_DELAY_MS` | server | 800 | 1200–1500 | Soniox tolerates longer intra-utterance pauses before forcing `<end>`. |
| `SONIOX_STT_ENDPOINT_SENSITIVITY` | server v5 | unset | try `0.3–0.4` (lower) | Model is more conservative about declaring the turn over. The most targeted lever for mid-sentence cutoffs. |
| `SONIOX_STT_ENDPOINT_WAIT_S` | backend | 0.4 → **1.5** (done) | 1.0–1.5 | Backend waits for a natural Soniox endpoint before force-finalizing after client `audio_end`. |

Trade-off: every knob you raise adds to **turn-end latency** (silence-to-answer).
The perceived lag at turn-end ≈ `min(client redemptionMs, soniox endpoint delay)`
plus the backend grace/finalize. Push these up only until mid-sentence cutoffs
stop; do not max them all out or every answer feels sluggish. Recommended order:
(1) bump `redemptionMs` first (cheapest, client-local), (2) then lower
`endpoint_sensitivity`, (3) raise `max_endpoint_delay_ms` only if pauses are still
truncated, (4) keep `ENDPOINT_WAIT_S` ≈ the client `redemptionMs` so the backend's
patience matches the client's.

### (a) Per-language considerations (en / ru / kk / zh)

`language_hints=en,ru,kk,zh` with `language_hints_strict=true`. Endpoint timing is
acoustic/linguistic, so it is not uniform across languages:

- **Lower-resource languages (kk — Kazakh)** are more likely to endpoint
  *differently*: the model has weaker language priors, so it may either cut early
  on unfamiliar word boundaries or hesitate. If `kk` users report cutoffs, a lower
  `endpoint_sensitivity` and a slightly higher `max_endpoint_delay_ms` help most.
- **ru / zh** with code-switching (mixed Russian + AIFC English terms, Chinese
  with Latin acronyms like AFSA/AIX) can trigger early endpoints at script
  boundaries. The AIFC `context.terms` list (in `stt.py`) already biases the model
  toward these terms, which indirectly reduces spurious endpoints.
- Soniox endpoint config is **per-session, not per-language**, so we cannot set
  different `max_endpoint_delay_ms` per hint in one session. The practical move:
  tune the global values to be safe for the weakest language (kk), and accept that
  en turn-end will be marginally less snappy. If that trade hurts, the only true
  per-language path is opening separate sessions per detected language (not worth
  it for this demo).

### (b) Interaction with the client-side VAD (the two-endpointer race)

The frontend VAD (`activeListeningConfig.ts`) and Soniox are **independent
endpointers**:

- Client VAD: `positiveSpeechThreshold` 0.40, `negativeSpeechThreshold` 0.28
  (~0.12 hysteresis so a brief mid-word dip doesn't end speech), `minSpeechMs`
  500, `redemptionMs` 800. On `onSpeechEnd` (`App.tsx`) it sends `audio_end`.
- That `audio_end` triggers the backend `handle_audio_end()` path above, where
  `SONIOX_STT_ENDPOINT_WAIT_S` now buys 1.5 s of patience.
- Independently, Soniox may emit `<end>` first → `on_realtime_final` →
  `process_final_transcript` (the `claim_finalization()` guard ensures only one of
  the two paths actually finalizes a given utterance).

**Key implication of raising `ENDPOINT_WAIT_S`:** it only buys patience on the
*client-VAD* path. If the client VAD's `redemptionMs` (800 ms) is shorter than the
real pause length, the client still fires `audio_end` early — the backend then
waits 1.5 s, which is good (user can resume), but the *experienced* turn-end on a
genuine stop is now `redemptionMs` + however long the backend grace takes to
resolve. So raising `ENDPOINT_WAIT_S` without also nudging `redemptionMs` up means
genuine turn-ends can feel slightly slower (backend waiting out the grace). Keep
the two roughly aligned.

The backend barge-in gate (`backend/utils/language.py` `is_interrupt_candidate`,
used in `stt.py` `_send_partial`) is a *separate* concern — it decides whether a
meaningful partial during playback should interrupt the avatar
(`_MIN_INTERRUPTING_ALPHA_WORDS=3`, `_MIN_INTERRUPTING_ALPHA_CHARS=7`). It is not
an endpointer, but note that a more conservative (slower) endpoint means partials
linger longer, which can make barge-in feel marginally more eager; no change
needed, just be aware when tuning both.

### What to measure (log markers)

Tune empirically off the structured `log_event` markers (in `var/backend.log`):

- `stt_partial_received` — live partial text + char count. Watch the last partial
  before a cutoff: if it ends mid-phrase, the endpoint fired too early.
- `stt_final_received` (marker `<end>`/`<fin>`, char count, `audio_bytes`,
  `audio_chunks`) — **the endpoint fired here.** Compare its timestamp/char count
  to the last `stt_partial_received`. A large gap of dropped trailing speech = the
  endpoint preceded the user finishing.
- `stt_finalize_send` / `stt_finalize_done` (`latency_ms`) — fired on the
  force-finalize path; if you see these frequently it means the client VAD path is
  forcing finalization (i.e., Soniox did **not** endpoint naturally within
  `ENDPOINT_WAIT_S`). Frequent force-finalizes after raising `ENDPOINT_WAIT_S`
  suggest `max_endpoint_delay_ms` / `endpoint_sensitivity` are still too eager, or
  the client `redemptionMs` is too short.
- `stt_final` (session.py, `latency_ms`) — total STT turn latency; this is the
  number that grows if you over-tune the patience knobs. Use it as the turn-end
  lag budget.
- `stt_realtime_reused_after_final` / `_after_audio_end` — confirm which path won
  the race for a given turn.

Procedure: collect a batch of real turns, grep `stt_final_received` vs the
preceding `stt_partial_received`. If trailing speech is being dropped, lower
`endpoint_sensitivity` / raise `max_endpoint_delay_ms` first; if `stt_finalize_send`
dominates, the client VAD is finalizing too soon → raise `redemptionMs`. Keep
`stt_final` `latency_ms` within an acceptable turn-end budget (target: keep the
silence-to-answer feel under ~1.2–1.5 s on a genuine stop).
