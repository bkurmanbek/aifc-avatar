# Implementation Ideas — from the Soniox voice-bot demo analysis

Three ideas drawn from reading the Soniox voice-bot demo server
(`apps/soniox-voice-bot-demo/server`) and comparing it to our pipeline.

Of the three, only **#1** and **#2** are things to actually build. **#3** is a
*validation* — confirmation that what we already do is correct (nothing to
implement). A real "third candidate" (a structural refactor) is listed at the
end.

Status: **explanation only — no code changes made.**

---

## #1 — Two-stage barge-in (fast VAD-stop + text-commit)

### What we do today
Barge-in fires only once the backend has a **meaningful transcribed partial**
(`on_meaningful_partial` → `is_interrupt_candidate` in `utils/language.py`). The
avatar keeps talking through this chain: client VAD catches speech → audio sent →
Soniox transcribes → backend confirms it's a real query → `interrupt()`. Robust
(few false positives) but ~0.5–1 s elapses between "user opens mouth" and "avatar
goes quiet." Feels slightly deaf.

### What the Soniox demo does
It ducks/cancels TTS the moment VAD sees **speech start**
(`UserSpeechStartMessage`), before any words are recognized — instant. Any STT
text is a secondary confirmation.

### Proposed change — split into two stages
- **Stage 1 — instant, on VAD `onSpeechStart` (while the avatar is speaking):**
  not a hard stop, but a **reversible duck/pause of the avatar audio** locally on
  the client. Gives immediate "I heard you" feedback.
- **Stage 2 — on meaningful STT partial (as today):** full `interrupt()` + new
  turn. If no meaningful text arrives within a short window (~600–800 ms) — it was
  noise/echo/a cough — **restore the volume** and let the avatar finish. No false
  interruption.

### Why duck (not hard-stop) at Stage 1
A hard cancel on VAD-start can't be undone — frames and audio are already
cancelled, so you can't resume after a false trigger. Stage 1 must be soft and
reversible (lower gain or pause playback) **without** cancelling the backend
pipeline. The hard cancel happens only at Stage 2.

### Main risk — echo
VAD can fire on the avatar's own voice from the speakers → false duck. Mitigations:
(a) `echoCancellation` is already on; (b) ducking (not pausing) makes a false
trigger barely noticeable; (c) committing a new query still requires meaningful
text, so echo never launches a false question.

### Where it lives
- `frontend/src/hooks/useChunkPlayback.ts`: sources currently do
  `src.connect(ac.destination)`. To duck, insert a **`GainNode`** in the chain
  (`src → gain → destination`) and ramp `gain.gain` with `setTargetAtTime`.
- `frontend/src/App.tsx` `onSpeechStart`: if `isBusy/playing` → lower gain
  (Stage 1) + start a restore timer.
- Existing backend barge-in stays as Stage 2; on `interrupted` → full
  `stopPlayback()` as today.

### Effort / impact / risk
Impact: high (interruption feels instant). Effort: medium (GainNode + restore
logic). Risk: echo-induced ducking → ship behind a flag and test.

---

## #2 — Lean on Soniox server-side endpoint detection for turn-end

### What we do today
Turn-end is driven by the **client VAD silence timer** (`redemptionMs` = 800 ms
of silence → `onSpeechEnd` → `sendAudioEnd` → backend finalizes STT). Simple and
predictable, but "dumb" — can cut off on a thinking pause and ignores whether the
thought is actually complete.

### What the Soniox demo does
Enables `enable_endpoint_detection` and trusts **Soniox** to decide turn-end via
an `<end>` token. That's a model-based decision (prosody/semantics), not just N ms
of silence: ends faster after a complete phrase, rides through mid-sentence pauses.

### Proposed change
Shift turn-end onto **Soniox endpointing** instead of (or in addition to) the
client silence timer:
- We already have the knobs: `SONIOX_STT_ENDPOINT_SENSITIVITY` (currently
  empty/default) plus legacy `_MAX_ENDPOINT_DELAY_MS` / `_ENDPOINT_WAIT_S`.
- Option A (soft): raise client `redemptionMs` (so the client doesn't cut early)
  and finalize the turn on the Soniox endpoint signal, tuning
  `SONIOX_STT_ENDPOINT_SENSITIVITY` (higher = end sooner/snappier, lower = wait
  longer).
- Option B (A/B test): just experiment with `endpoint_sensitivity` and compare the
  "cuts me off / lags" feel.

### Trade-off
Client silence timer = predictable. Soniox endpoint = smarter but a black box: bad
tuning can fire too early (clips) or too late (slow reply). So: only via the
`sensitivity` knob + measurement.

### Where it lives
`backend/media/stt.py` (`_soniox_config`) + `config.env`
(`SONIOX_STT_ENDPOINT_SENSITIVITY`); minor logic to act on the endpoint signal;
`frontend/src/activeListeningConfig.ts` for `redemptionMs`.

### Effort / impact / risk
Impact: medium (more natural pauses / faster reply after a complete phrase).
Effort: low (mostly config + small reaction logic). Risk: low-medium (sensitivity
tuning).

---

## #3 — TTS-cancel pattern: validation, nothing to implement

This was not an idea to build but a **confirmation**. The Soniox demo code states
outright:

> *"Soniox doesn't have support for cancelling in-progress TTS streams"* — so they
> just null the `stream_id` and stop sending further chunks; already-sent audio
> can't be recalled; **the client stops playback.**

That is exactly what we do: on interrupt the backend cancels the TTS worker
(`ResponseStream.cancel_all()`), and the frontend stops on the `interrupted` event
(`stopPlayback()`). So there is no better server-side TTS cancel from Soniox and
we aren't missing anything. **Nothing to implement — this is a "done right" check.**

---

## Real "third candidate" (optional structural improvement): message bus

In the Soniox demo the processors (STT, VAD, LLM, TTS) are fully decoupled through
a single message queue — each reacts only to the message types it cares about. Our
`session.py` (1000+ lines) and `App.tsx` each do many things at once — which
matches the "god-object" notes from the earlier code review.

- **Gives:** cleaner code, easier testing, easier to add new reactions (e.g. the
  two-stage barge-in from #1 would slot in naturally).
- **Does NOT give:** any user-facing functional win — purely maintainability.
- **Effort/risk:** large effort, real regression risk in a working pipeline. Only
  as a separate, unhurried pass — not before a demo.

---

## Priority summary

| Idea | Impact | Effort | Risk |
|---|---|---|---|
| #1 two-stage barge-in | high (responsiveness) | medium | echo → behind a flag |
| #2 Soniox endpointing | medium (naturalness) | low | sensitivity tuning |
| message bus refactor | maintainability only | high | regressions |

Most user-visible: **#1**. Next step on approval: prototype #1 behind a flag
(GainNode + restore) to compare the interruption feel live, and/or quickly trial
#2 via `SONIOX_STT_ENDPOINT_SENSITIVITY`.
