# Implementation Ideas — from the Soniox voice-bot demo analysis

Three ideas drawn from reading the Soniox voice-bot demo server
(`apps/soniox-voice-bot-demo/server`) and comparing it to our pipeline.

Of the three, only **#1** is a thing to actually build. **#2** turned out to be
**already implemented** (verified in code) — it's now a measure-and-tune task, not a
feature. **#3** is a *validation* — confirmation that what we already do is correct.
A real "third candidate" (a structural refactor) is listed at the end.

Status: **explanation only — no code changes made.**

---

## #1 — Reversible output-buffer barge-in (first-word pause + 3-word commit)

> Refined design. The naive "duck on VAD speech-start" version was **rejected** — see
> "Why not the VAD-duck version" below. This version gets word-1 responsiveness while
> staying noise-robust, by making an early stop *reversible*.

### What we do today
Barge-in fires only once the backend has a **meaningful transcribed partial**
(`on_meaningful_partial` → `is_interrupt_candidate` in `utils/language.py`, which
needs `_MIN_INTERRUPTING_ALPHA_WORDS = 3` real words / `_MIN_INTERRUPTING_ALPHA_CHARS
= 7` letters — single stop-words like "stop"/"стоп" already interrupt on one word).
So for a normal query the avatar keeps talking until you've said **~3 words** *and*
Soniox has transcribed them. That word count is most of the ~0.5–1 s delay; it feels
slightly deaf.

### Why the 3-word wait exists (the real constraint)
A stop today is **irreversible** — `cancel_all()` discards the avatar's remaining
audio/frames. So we must be *sure* before stopping, hence the conservative 3-word
bar. The delay is the price of irreversibility, not of transcription speed. (Input
buffering — the preroll ring buffer, already flushed on `onSpeechStart` — only fixes
onset clipping; it cannot make 3 spoken words arrive faster.)

### Why NOT the VAD-duck version
Ducking/pausing on raw VAD `onSpeechStart` reacts to **audio energy**, so in a noisy
room (chatter, other people, ambient speech-like sound) the VAD fires constantly →
the avatar's audio dips/pauses **all the time** — worse than the current deafness.
`echoCancellation` removes the avatar's *own* voice but not external noise. Rejected.

### Refined proposal — buffer the avatar OUTPUT so an early stop is reversible
1. **Gate on the first transcribed WORD, not raw VAD.** A real word means Soniox
   transcribed something speech-like → far rarer on noise than VAD energy.
2. **On the first word → soft "hold":** *pause* avatar playback and **retain** the
   not-yet-played audio/frames in a buffer (do NOT `cancel_all()`). The avatar stops
   **instantly** — responsive after 1 word instead of 3.
3. **Confirmation window (~300–500 ms)** waiting for the existing 3-word
   `is_interrupt_candidate` bar:
   - **Confirmed** → discard the held buffer, tear down, start the new turn. (The
     3-word wait is now invisible — we already stopped on word 1.)
   - **Not confirmed** (noise / a half-word that never grew) → **resume from the
     buffer**; held audio/frames play out seamlessly, nothing lost.

The output buffer is exactly what makes the aggressive early pause undo-able, so we
can act on word 1 yet only *commit* (irreversibly) at word 3.

### Noise behaviour (honest)
Much better than the VAD-duck version (which trips on any sound) because the pause is
gated on a transcribed word and a stray noise-word fails the 3-word confirm → resume,
no false new turn. **But not zero:** in a *very* noisy room where Soniox emits spurious
first-words often, you'd still see occasional pause→resume **flicker** (the avatar
briefly hitches, then continues). Flicker rate tracks how often noise fakes a real word.

### Where it lives
- `frontend/src/hooks/useChunkPlayback.ts` — a **soft pause/resume**: stop advancing
  the `audioCursorRef` and hold the frame schedule (retain buffered chunks) instead of
  `stopPlayback()`; resume replays from the held position.
- `backend/session/session.py` — on first meaningful word send a `hold` event (do NOT
  `interrupt()` yet); on 3-word confirm → real `interrupt()` + new turn; on
  confirmation-window timeout → `resume` event.
- Reuses the existing `is_interrupt_candidate` 3-word bar as the *commit* gate (no
  change there); adds only the earlier "first word → hold" trigger.

### Simpler fallback (no buffering)
If the deployment is a quiet kiosk, just **lower the thresholds** —
`_MIN_INTERRUPTING_ALPHA_WORDS = 2`, `_MIN_INTERRUPTING_ALPHA_CHARS = 5` (or 1 word) in
`language.py`. Two-line change, no frontend work, no flicker; cost is a slightly higher
chance of a false interrupt on a single mis-transcribed noise word. Stays on the
noise-robust text axis (reacts to words, not energy).

### Effort / impact / risk
Impact: high (word-1 responsiveness, noise-safe commit). Effort: medium (soft
pause/resume in the playback hook + hold/resume events; the harder part is making the
audio-cursor pause/resume gapless). Risk: pause→resume flicker in heavy noise; ship
behind a flag and A/B against the simpler threshold-lowering.

---

## #2 — Soniox endpoint detection: ALREADY LIVE — measure & reconcile the dual endpointers

> Correction (verified in code): the original framing ("adopt Soniox endpointing
> instead of the client silence timer") was **wrong** — Soniox endpointing is already
> enabled and already drives turn-end. This is a tuning/reconciliation question, not a
> feature to build.

### What we actually do today (BOTH, racing)
Turn-end is decided by **two endpointers running concurrently**, with
`claim_finalization()` as the race guard so only the first one wins per turn:
1. **Client VAD silence timer** — `onSpeechEnd` after `redemptionMs = 800 ms` of
   silence → `audio_end` → `session.py handle_audio_end` → finalize. Purely
   silence-based ("dumb"): cuts on a thinking pause.
2. **Soniox server-side endpointing** — `SONIOX_STT_ENABLE_ENDPOINT_DETECTION = True`
   (default), sent to Soniox with `max_endpoint_delay_ms = 800`. When Soniox emits
   **`<end>`** (`stt.py:176–224`), it calls `_on_final_utterance(...)` → starts the
   answer pipeline. Model-based (prosody/semantics): can ride through mid-sentence
   pauses, end faster after a complete thought.

So the Soniox-demo behaviour is **already implemented**. What we do NOT know is which
endpointer usually wins — and that determines whether we actually get the smart
behaviour or whether the 800 ms client VAD pre-empts it (giving silence-based
endpointing despite Soniox being on).

### The real task: measure first, then reconcile
- **Measure (cheap, no code):** the logs already distinguish the two —
  `stt_final_received marker=<end>` means Soniox won; the `handle_audio_end` path means
  client VAD won. Count which dominates over real turns.
- **If client VAD pre-empts** (likely, both ~800 ms but VAD is purely silence): to get
  Soniox's "ride through pauses" benefit, **raise client `redemptionMs`** so it doesn't
  cut early and let Soniox lead, and/or set `SONIOX_STT_ENDPOINT_SENSITIVITY` (currently
  unset = Soniox default; higher = snappier, lower = waits longer).
- **Risk of letting Soniox lead:** if Soniox is slow/uncertain (esp. weaker on kk/zh),
  turn-end could feel laggy; the raised `redemptionMs` removes the safety net. So tune +
  measure per language, don't just flip it.

### Where it lives
Knobs already exist: `backend/settings.py`
(`SONIOX_STT_ENABLE_ENDPOINT_DETECTION`, `SONIOX_STT_MAX_ENDPOINT_DELAY_MS`,
`SONIOX_STT_ENDPOINT_SENSITIVITY`, `SONIOX_STT_ENDPOINT_WAIT_S`) wired in
`backend/media/stt.py`; `frontend/src/activeListeningConfig.ts` for `redemptionMs`.
No new wiring — just config + a log-analysis pass.

### Effort / impact / risk
Impact: low–medium (only if the dual-path is currently mis-balanced). Effort: very low
(measure logs, then tune config). Risk: low-medium (per-language endpoint quality).
**Lower priority than first thought — it's already built; verify it's behaving, don't
rebuild it.**

---

## #3 — TTS-cancel pattern: validation, nothing to implement

This was not an idea to build but a **confirmation**. The Soniox demo code states
outright:

> *"Soniox doesn't have support for cancelling in-progress TTS streams"* — so they
> just null the `stream_id` and stop sending further chunks; already-sent audio
> can't be recalled; **the client stops playback.**

That is exactly what we do: on interrupt the backend cancels the TTS worker
(`ResponseStream.cancel_all()`, which also stops pulling further audio), and the
frontend stops on the `interrupted` event (`stopPlayback()`). So there is no better
server-side TTS cancel from Soniox and we aren't missing anything. **Nothing to
implement — this is a "done right" check** (verified against our code).

**One nuance (cost, not correctness):** an interrupted turn still *bills* for the TTS
(and LLM) tokens already generated before the cancel — you can't un-generate audio that
was already produced. `cancel_all()` already minimizes this by stopping as early as
possible; it's inherent, unavoidable, and minor. No action.

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
| #1 reversible output-buffer barge-in | high (word-1 responsiveness) | medium | pause-flicker in heavy noise → flag |
| #1 fallback: lower interrupt thresholds | medium (word-2 vs word-3) | tiny (2 lines) | false interrupt on mis-transcribed noise |
| #2 Soniox endpointing (already live) | low–med (only if mis-balanced) | very low (measure+tune) | per-language endpoint quality |
| message bus refactor | maintainability only | high | regressions |

Most user-visible: **#1**. Next step on approval: either the **simple fallback**
(lower `_MIN_INTERRUPTING_ALPHA_WORDS`/`_CHARS` in `language.py`, ~2 lines) for a
quick snappier-stop A/B, or prototype the **soft pause/resume** (first-word hold +
3-word commit + buffer resume) behind a flag to compare the interruption feel live.
Independently for #2: it's already live, so first **measure the log split**
(`stt_final_received marker=<end>` vs `handle_audio_end`) to see which endpointer wins,
then tune `redemptionMs` / `SONIOX_STT_ENDPOINT_SENSITIVITY` only if mis-balanced.
