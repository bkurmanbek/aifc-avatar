# Code Review — avatar-system-2

> **RESOLUTION (2026-06-17).** Triaged + verified against the code, then fixed.
> **Fixed:** #1 (guard `finally` with `is current_task` + `interrupt()` clears turn state),
> #2 (key `winner_already_streamed` off `spoken_streaming_active` + mismatch log), #5 (gate
> evicts under lock, networked `force_disconnect` runs outside it as tracked tasks), #6
> (strong-ref `_drain_tasks` set), #7 (combine UTF-16 surrogate pairs — tested with emoji),
> #8 (`threading.Lock` single-flight on the video build + re-check), #10 (ensureBitmapReady
> registers in `bitmapPending`; preloadBitmap closes-before-overwrite), #12 (FAQ fast-path for
> all langs), #14 (revoke intro object URL on unmount), #15 (log the real timeout budget),
> #3 (busy → steady `BUSY_RECONNECT_MS` retry, no fast-base reset on busy, +jitter), #4 (only
> tear down the shared TTS socket when it's actually dead).
> **Refuted (not bugs, left as-is):** #9 (increment is after the queue; tail is padded),
> #11 (frame loop shares `ch.scheduledT0` with audio → per-chunk aligned, not drift),
> #13 (WS messages are atomic; malformed frames already caught/dropped).


Whole-codebase review (recall mode, xhigh effort). The standout is **#1**, a regression introduced by the latest commit `9a32246` ("Fix interrupt-hang / WS-disconnect / chunk-gap"). Findings #2–#15 are pre-existing issues found while reviewing the broader codebase as requested.

Severity legend: 🔴 critical · 🟠 high · 🟡 medium · 🟢 low

---

## #1 — `pipeline_task` clobber (regression in this commit) 🔴

**Where:** `backend/session/session.py` — `finally` in `run_query` (line 1027) and `run_intro` (line 447), interacting with the new `interrupt()`.

**What the code does.** Previously `interrupt()` did `await self.pipeline_task` — it waited for full teardown of the cancelled task (including its `finally`) before returning. The new commit changed this to avoid blocking the WS/STT loop:

```python
done, pending = await asyncio.wait({task}, timeout=_INTERRUPT_TEARDOWN_TIMEOUT_S)  # 1.5s
if pending:
    asyncio.create_task(self._drain_cancelled(task))  # drain in background, return immediately
```

**Why it's a bug.** Sequence:

1. Turn A is running, `self.pipeline_task = task_A`. SyncTalk wedges (GPU contention).
2. New query arrives → `interrupt()` sets `self.pipeline_task = None`, cancels `task_A`, waits 1.5s. `task_A` hasn't finished teardown → goes to background drain, `interrupt()` **returns**.
3. `handle_text`/`start_turn` assigns `self.pipeline_task = task_B` (new `run_query`).
4. Later `task_A` reaches its `finally` and runs `self.pipeline_task = None` (line 1027) — **clobbering the live `task_B`**.

**Consequences.** Now `self.pipeline_task is None` while turn B is actually running:
- `on_meaningful_partial` and `interrupt()` early-return on `if self.pipeline_task is None` → **barge-in and the Stop button silently stop working** for turn B.
- The next query sees `pipeline_active = False`, doesn't call `interrupt()`, and starts a **second concurrent `run_query`** — two pipelines contend for the single GPU, the exact thing the single-session design prevents.

Pre-commit this was impossible: `interrupt()` awaited A's `finally` before B was assigned.

**Fix.** Only null the field if it still points at this task:
```python
finally:
    if self.pipeline_task is current_task:   # current_task = this run_query's task
        self.pipeline_task = None
        self.active_metrics = None
        self.active_turn_id = None
```
(Same for `active_metrics`, `active_turn_id`, `writer.clear_active_turn`.)

---

## #2 — Double-emit of `spoken` text 🔴

**Where:** `backend/session/session.py:938-944`.

**What the code does.** In the public race, `faq`, `cache`, and `gemini_local_rag` run concurrently. Gemini streams chunks of the `"spoken"` field via `on_spoken_delta`, which feeds TTS immediately and sets `spoken_streaming_active = True`. After the race:

```python
winner_already_streamed = (
    spoken_streaming_active
    and race_result is not None
    and race_result.winner.source in ("gemini_local_rag", "external_internal_rag")
)
if not winner_already_streamed:
    await stream.emit_spoken_text(spoken)
```

**Why it's a bug.** The guard requires the **winner** to be one of two sources, but the winner can be different:
- Gemini already streamed the first `"spoken"` chunks (TTS already talking), then **`faq`/`cache` wins** (they're fast); or
- Gemini misses the deadline and `winner = fallback_message`.

In both cases `winner.source` ∉ the whitelist → `winner_already_streamed = False` → `emit_spoken_text(spoken)` re-sends the full winner text **on top of the already-spoken Gemini fragment**. The avatar speaks part of the Gemini answer, then the full FAQ answer (or a contradictory "Sorry, I couldn't find…"). Doubled/garbled audio.

The window is small (FAQ/cache usually win before Gemini streams), but it's a real race.

**Fix.** Key off the fact that streaming happened, not the winner's source: if `spoken_streaming_active`, either skip `emit_spoken_text` or emit only the delta (final winner text minus what was already streamed).

---

## #3 — Reconnect storm on `busy` 🔴

**Where:** `frontend/src/hooks/useWebSocket.ts:202`.

**What the code does.** When the pipeline is busy, the server first *accepts* the WS (`onopen` fires), then sends `{type:'busy'}` and closes with code 1013. In `onopen`:
```js
ws.onopen = () => {
  ...
  reconnectDelayRef.current = RECONNECT_BASE_MS   // reset to 1500ms
  ...
}
```

**Why it's a bug.** The exponential backoff grows in `scheduleReconnect`, but `onopen` resets it every attempt. For a busy client each cycle is: socket opens → `onopen` resets delay to base → server closes → `scheduleReconnect` uses the just-reset base → reconnect in ~1.5s. Forever, **every 1.5s**, with no growth.

**Consequences.** Every waiting visitor hammers the H200 box with connect→acquire→close cycles every 1.5s. With several waiting clients this is constant load — exactly what backoff was meant to smooth.

**Fix.** Don't unconditionally reset `reconnectDelayRef` in `onopen`. Reset only after the connection proves useful (first non-busy app message), or treat `busy` as a special close that grows the delay.

---

## #4 — Closing the shared TTS socket kills parallel streams 🟠

**Where:** `backend/media/tts.py:289` (`_send_config_with_retry` / `_close_ws`).

**What the code does.** The TTS client multiplexes independent streams by `stream_id` over a **single** WebSocket. The avatar pipeline runs 2 parallel SyncTalk workers, each with its own TTS stream. On a transient config-send error the code calls `self._close_ws()` + `_drain_queue(...)`.

**Why it's a bug.** `_close_ws()` closes the **single** `self._ws` shared by all streams. If stream B's send stumbles, closing the socket also kills stream A: its `reader_loop` ends, `_fail_pending` injects a connection error into A's queue, and A's synthesis aborts mid-sentence — though A had no problem. Any transient send hiccup on one segment kills the sibling.

**Fix.** Distinguish "per-stream error" from "socket dead." Don't tear down the shared socket for one stream's failure; retry/fail only that `stream_id`, and reserve full `_close_ws` for a real transport break.

> Line 289 is from the finder pass — mechanism is sound, verify the exact line when fixing.

---

## #5 — `force_disconnect` awaited while holding the gate lock 🟠

**Where:** `backend/session/session_gate.py:70`.

**What the code does.** Inside `acquire()`, under `async with self._lock`, while evicting stale/dead slot holders:
```python
with contextlib.suppress(Exception):
    await e.session.force_disconnect("evicted")
```
`force_disconnect` does `await self.writer.send(...)` then `await self.websocket.close(...)` on the evicted session's socket.

**Why it's a bug.** If the evicted session has a half-open/hung socket, its `send`/`close` blocks until timeout. The whole time `self._lock` is held, and *all* `acquire()` (connecting client) and `release()` (disconnecting client) serialize on it. One hung socket turns the gate into a global serialization point that can wedge admission for everyone.

**Fix.** Don't call the networked `force_disconnect` under the lock. Under the lock just remove the entry from `self._active`; do the actual disconnect outside the lock (or in a background task with a timeout).

---

## #6 — `_drain_cancelled` created as fire-and-forget 🟠

**Where:** `backend/session/session.py:510`.

**What the code does.**
```python
asyncio.create_task(self._drain_cancelled(task))
```
The returned task isn't stored anywhere.

**Why it's a bug.** asyncio keeps only a *weak* reference to tasks. With no strong reference, the GC can destroy this task **before** `await task` completes. Then the cancelled pipeline's exception is never retrieved (possible "Task was destroyed but it is pending" warning), and SyncTalk teardown is cut short — defeating the drain's own purpose.

**Fix.** Hold a strong reference:
```python
self._drain_tasks: set[asyncio.Task] = set()
...
t = asyncio.create_task(self._drain_cancelled(task))
self._drain_tasks.add(t)
t.add_done_callback(self._drain_tasks.discard)
```

---

## #7 — Surrogate pairs in `_SpokenFieldExtractor` 🟡

**Where:** `backend/pipeline/answer_sources.py:125`.

**What the code does.** The streaming `"spoken"` parser decodes each `\uXXXX` escape independently:
```python
if len(self._ubuf) == 4:
    return chr(int(self._ubuf, 16))
```

**Why it's a bug.** Non-BMP characters (e.g. emoji 😀) are JSON-encoded as a **surrogate pair** of two escapes: `😀`. Decoded separately, this yields `chr(0xD83D)` and `chr(0xDE00)` — two *lone surrogates* (`'\ud83d'`, `'\ude00'`), not one character. These flow into `on_spoken_delta` → TTS. A later `.encode('utf-8')` (TTS request body, logging) raises `UnicodeEncodeError: 'utf-8' codec can't encode character '\ud83d'` — killing the segment or the whole turn. The non-streaming `json.loads` path decodes the same input correctly, so stream and final text diverge.

**Fix.** In the `in_unicode` state, when you get a high surrogate (0xD800–0xDBFF), don't emit immediately — wait for the next `\u` low surrogate (0xDC00–0xDFFF) and combine: `chr(0x10000 + ((hi-0xD800)<<10) + (lo-0xDC00))`.

---

## #8 — Race when building the intro MP4 concurrently 🟡

**Where:** `backend/intro.py` (≈ line 462), `build_intro_video`.

**What the code does.** Temp file names (`tmp_audio`, `tmp_out`, `tmp_err`) are derived **only from the avatar key**, e.g. `out_path.with_suffix('.audio.wav')` and `…mp4.tmp`. Two entry points build: `prebuild_intro_cache` (startup) and `ensure_intro_video` (first session, via `asyncio.to_thread`).

**Why it's a bug.** Both can build the **same avatar** to the **same** temp paths simultaneously. One process writes `tmp_audio` while the other's ffmpeg reads it; both call `tmp_out.replace(out_path)`. The loser may replace the output with a half-written/killed file or unlink a path the winner still needs → a corrupt or missing `intro.mp4`. The existing `asyncio.Lock` only guards audio generation, not video assembly.

**Fix.** A shared lock / single-flight on the build keyed by avatar, or per-process unique temp paths (PID/uuid) with an atomic `replace` at the end.

---

## #9 — Silent dropped chunk from `segments_emitted` mismatch 🟡

**Where:** `backend/pipeline/response_stream.py:287` (and the early return in `_queue_pcm_segment`, ≈412).

**What the code does.** In `flush_segment`, `segments_emitted` is incremented **before** `await _queue_pcm_segment(...)`. But `_queue_pcm_segment` returns early if `len(pcm) < 2` (a tiny trailing buffer), queuing nothing.

**Why it's a bug.** A tiny final flush makes `segments_emitted == 1` even though no segment was queued. Then:
- the "TTS returned no audio" error branch (gated on `segments_emitted == 0`) is **suppressed**;
- no audio/frame segment was actually produced.

The client waits on a chunk that never arrives — a **silent failure with no `media_error`**, and the turn can hang in "speaking."

**Fix.** Increment `segments_emitted` only after a segment is actually queued (after a successful `_queue_pcm_segment`), or have it return a success flag.

> Not fully traced during review — flagged as plausible; verify exact lines when fixing.

---

## #10 — `ImageBitmap` leak from double decode 🟡

**Where:** `frontend/src/hooks/useChunkPlayback.ts:231` (vs `preloadBitmap`, ≈426).

**What the code does.** On a cold-start chunk, `ensureBitmapReady` decodes frames 0–2 while the `preloadBitmap` loop (`PRELOAD_FRAME_WINDOW`) decodes the same frames. `preloadBitmap`'s `.then` unconditionally does:
```js
ch.bitmapCache[frameIdx] = bitmap
```
without checking for an existing entry or closing the old one.

**Why it's a bug.** Two `ImageBitmap`s get decoded for one `frameIdx`. `preloadBitmap` overwrites the already-cached one (from `ensureBitmapReady`) **without `.close()`** → an orphaned GPU-backed bitmap that lives until GC. Several per cold-start chunk; over ~20 chunks/turn and a long kiosk session this accumulates, causing the GC hitches the eviction logic tried to avoid.

**Fix.** Before writing `bitmapCache[frameIdx]`, check for an existing value and `.close()` it (or don't overwrite). Also check `bitmapPending` to avoid decoding the same frame twice.

---

## #11 — Audio drift from scheduling "into the past" 🟡

**Where:** `frontend/src/hooks/useChunkPlayback.ts:284` (`scheduleChunkAudio`).

**What the code does.**
```js
const t0 = cursor > currentTime + 0.02 ? cursor : currentTime + 0.08
// ...
audioCursorRef.current = t0 + duration
```
If `audioCursorRef` has fallen behind the AudioContext clock (render jitter, tab throttling, decode stall), it uses `currentTime + 0.08`, then sets the cursor to `t0 + duration` — **without accounting for the skipped gap**.

**Why it's a bug.** The next chunk's audio jumps forward (audible seam), while the frame render loop drives frames off `elapsed = currentTime − t0` per chunk. The accumulated offset pushes **audio ahead of the frame timeline** — lips and sound desync for the rest of the turn until `stopPlayback` resets the cursor.

**Fix.** When forced to schedule forward, account for the shift honestly: resync the frame loop's `t0` along with audio, or fold the skip into the shared time base so frames and audio share one origin.

---

## #12 — FAQ fast-path is English-only 🟡

**Where:** `backend/knowledge/rag.py:108`.

**What the code does.** `_retrieve_with_fast_path` gates the FAQ fast-path behind `if language == 'en'`, even though `_FAQ_ENTRIES` contain ru/kk/zh entries and `_faq_fast_path_lookup` accepts a language filter.

**Why it's a bug.** A Russian/Kazakh/Chinese user whose question exactly matches a localized FAQ entry **skips** the high-confidence (≥0.9) fast-path and falls through to full RAG+Gemini — extra latency and a higher chance of a worse/fallback answer, exactly where the FAQ cache was meant to help.

**Fix.** Drop the `== 'en'` restriction and pass `language` into `_faq_fast_path_lookup` for all supported languages.

> Note: there are two FAQ entry points. `faq_candidate` in `answer_sources.py` calls `_faq_fast_path_lookup` regardless of language. This fix concerns the `fast_answer_plan_retrieve` path in `rag.py`; unify both for consistent behavior.

---

## #13 — Binary frame `turnLen` not validated 🟡

**Where:** `frontend/src/hooks/useWebSocket.ts:251`.

**What the code does.** The frame parser trusts `turnLen` without checking `4 + turnLen <= byteLength`:
```js
const turnId = ... new Uint8Array(buf, 4, turnLen) ...
const jpeg = buf.slice(4 + turnLen)
```

**Why it's a bug.** A frame truncated by the transport (Cloudflare buffering, partial TCP) but with a valid `0xF1` header and a `turnLen` pointing past the buffer:
- either throws `RangeError` in `new Uint8Array(buf, 4, turnLen)` (caught, frame silently dropped);
- or `buf.slice(4 + turnLen)` returns a too-short JPEG that `createImageBitmap` later rejects.

Either way the chunk's delivered frame count falls below `expectedFrames`, throwing off `effectiveFps`/`frameDone` accounting → a visible hitch at that chunk.

**Fix.** Check `view.byteLength >= 4 + turnLen` before reading; if not, treat the frame as corrupt explicitly (and possibly compensate the chunk's frame count).

---

## #14 — Intro MP4 object URL never revoked 🟢

**Where:** `frontend/src/App.tsx:224`.

**What the code does.** The `URL.createObjectURL(blob)` result is stored in `introObjUrlRef.current` and reused for the page lifetime, but `URL.revokeObjectURL` is never called (`stopIntroVideo` only clears the `<video>` `src`).

**Why it's a bug.** The multi-MB intro-MP4 blob stays pinned in memory for the whole session. On a kiosk running for days with periodic intro replays this is a persistent leak; combined with the `ImageBitmap` leak (#10) it contributes to gradual memory growth that eventually degrades decode/render.

**Fix.** When the blob is no longer needed (or before reloading), call `URL.revokeObjectURL(introObjUrlRef.current)`.

---

## #15 — Timeout log prints the wrong constant 🟢

**Where:** `backend/pipeline/answer_race.py:104`.

**What the code does.** `wait_for` uses the budget `EXTERNAL_RAG_TIMEOUT_S + GEMINI_RAG_MAX_WAIT_MS/1000` (≈20s), but the warning prints a different constant:
```python
log.warning(
    "external RAG first response timeout after %.2fs query=%r",
    EXTERNAL_RAG_FIRST_RESPONSE_TIMEOUT_S,   # ≈8s — wrong value
    query[:160],
)
```

**Why it's a bug.** On a real timeout the log says "after 8.00s" though the operation was allowed ~20s. When triaging incidents on this load-bearing 20s budget (CLAUDE.md calls it out), an operator is pointed at the wrong knob and the printed duration is simply wrong. Not a crash, but a real source of misdiagnosis.

**Fix.** Log the actually-used value: `max(0.5, EXTERNAL_RAG_TIMEOUT_S + GEMINI_RAG_MAX_WAIT_MS / 1000)`.

---

## Refuted during verification

- **External-RAG negative-similarity "not_found" demotion** — the live `gemini_external_rag_candidate` ignores `result.confidence` and regenerates from chunks, so the hot path is unaffected (only the apparently-unused `external_rag_candidate` would see it).
- **Semantic-cache "no minimum threshold"** — mitigated by the `score < CACHE_WIN_THRESHOLD` gate in `cache_candidate`.
