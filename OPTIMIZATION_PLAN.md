# Avatar-System-2 — Performance & Scaling Optimization Plan

Goal: raise concurrent-session capacity and lower per-avatar cost on the existing
SyncTalk pipeline, **without regressing visual quality or realtime smoothness.**

This is a staged plan. Each phase is independent, flag-gated, and individually
revertible. Do them in order — later phases assume the benchmark harness and the
quality baselines from the earlier ones.

> **Hard rule:** all SyncTalk changes live in the **separate repo**
> `/home/admin-aifc/SyncTalk_2D` and require a **SyncTalk restart** to take effect.
> Backend/frontend restarts must NOT restart SyncTalk (see CLAUDE.md). Each change
> is committed in its own repo.

---

## 0. Context & current baseline

### Measured today (single H200)
| Metric | Value | Source |
|---|---|---|
| GPU | NVIDIA H200, 143 GB VRAM | `nvidia-smi` |
| SyncTalk instance footprint | ~7 GB VRAM | `nvidia-smi` compute-apps |
| GPU frame throughput | ~46–53 fps (1 session, 2 workers) | `avatar_chunk_done` logs |
| Realtime need per stream | 25 fps | fixed |
| **Concurrent realtime avatars** | **~2** | throughput / 25 |
| Per-frame transport | ~50 KB JPEG q82 × 25 fps ≈ **10 Mbps** | `_composite` |
| Client prebuffer | `LIVE_PREBUFFER_S ≈ 2.2 s` | `useChunkPlayback.ts` |
| Pipeline latency (good turn) | first_frame ~4.3 s, client_first_render ~6.3 s | `pipeline_done` metrics |
| Session gate | `MAX_CONCURRENT_SESSIONS = 1` | `session_gate.py` |

### The binding constraint
**GPU frame-generation throughput** (~50 fps) ÷ 25 fps/stream → ~2 concurrent
talking avatars. VRAM (143 GB), box uplink (~88 Mbps), backend CPU, and Vercel all
have more headroom than that. Therefore the optimization targets, in order of ROI:

1. **Model speed** (BF16, TensorRT) — raise the 50 fps ceiling → more avatars/GPU.
2. **Offload work** (FAQ video cache) — take common answers off the GPU entirely.
3. **Transport** (NVENC + WebCodecs) — cut the 10 Mbps stream → bandwidth/quality/
   multi-session enabler + modest latency.

### Relevant code map
| Area | File | Symbol |
|---|---|---|
| GPU forward (hot path) | `SyncTalk_2D/synctalk_server.py` | `_gpu_forward()` L176–186 |
| Warmup | same | L133–143 |
| CPU composite + JPEG | same | `_composite()` L260–275 |
| Cross-session batching | same | `FrameAccumulator` L279–335 |
| Streaming endpoint | same | `/infer_stream` L349–392 |
| Model | same | `net = Model(6,"ave")` L60–62 |
| Audio encoder | same | `audio_enc` L44–50 |
| ONNX export (exists!) | `SyncTalk_2D/unet_328.py` | `torch.onnx.export` ~L275–327 |
| Intro MP4 builder | `backend/intro.py` | `build_intro_video`, `ensure_intro_video` |
| FAQ / semantic cache | `backend/knowledge/faq.py`, `cache.py` | — |
| Realtime stream | `backend/pipeline/response_stream.py` | `ResponseStream` |
| Frame transport (WS) | `backend/api/ws_writer.py` | `send_frame_binary` |
| Frontend playback | `frontend/src/hooks/useChunkPlayback.ts` | scheduling, `LIVE_PREBUFFER_S` |
| Intro `<video>` | `frontend/src/App.tsx` | `loadIntroBlob`, `playIntroVideo` |

---

## Phase 0 — Benchmark harness & quality baseline (PREREQUISITE)

You cannot judge any optimization without a repeatable before/after. Build this
first; every later phase reuses it.

### 0.1 Throughput benchmark
- Script (`SyncTalk_2D/bench_infer.py`, new): POST a **fixed reference WAV** (e.g.
  ~15 s of speech) to `/infer` (non-streaming) N times, measure:
  - frames produced, wall time → **fps**
  - per-batch GPU time (instrument `_gpu_forward` with `torch.cuda.Event` start/stop)
  - peak VRAM (`torch.cuda.max_memory_allocated()`)
- Run with the GPU otherwise idle. Record median of 10 runs.

### 0.2 Quality baseline
- Render the reference WAV → dump the raw 320×320 UNet predictions (pre-composite)
  AND the final composited 540×960 frames to PNG, tagged `baseline_fp32/`.
- These are the golden frames. Every later phase compares against them with:
  - **SSIM** and **PSNR** on the 320 face crop (the part the model generates).
  - Visual diff on lips/teeth/jaw (where artifacts show first).
- Acceptance threshold (proposed): **SSIM ≥ 0.98, PSNR ≥ 38 dB** on the face crop,
  no visible lip artifacts. Tune after seeing FP32-vs-FP32 noise floor.

### 0.3 End-to-end latency baseline
- Use existing `scripts/capture_ws_mp4.py --query "What is AIFC?"` and the
  `pipeline_done` metrics to record: first_audio, first_frame, client_first_render,
  total. Capture 5 turns, keep the median.

### 0.4 Smoke-test suite (must stay green through every phase)
```bash
python scripts/smoke_ws_text.py          # basic text turn
python scripts/smoke_ws_interrupt.py      # barge-in / cancel
python scripts/capture_ws_tts.py --text "Hello" --out /tmp/rec.wav
python scripts/capture_ws_mp4.py --query "What is AIFC?" --output /tmp/turn.mp4
```

**Exit criteria for Phase 0:** reproducible fps/VRAM number, golden frames stored,
latency baseline recorded, all smoke tests green.

### 0.5 RESULTS — Phase 0 COMPLETE (2026-06-18)

Harness: `scripts/bench_synctalk.py` (standalone HTTP client, no server changes, no
restart). Reference WAV auto-built from `var/debug/tts_chunks` → `var/bench/reference.wav`
(28.68 s). Golden frames + results under `var/bench/`.

```bash
# baseline + golden:
python scripts/bench_synctalk.py --tag fp32_baseline --save-golden
# after an optimization:
python scripts/bench_synctalk.py --tag <name> --compare-golden
```

**FP32 baseline (isolated /infer_stream, GPU idle, 10 runs):**
| Metric | Value |
|---|---|
| fps median | **65.6** (min 63.0 / max 66.9) |
| realtime factor | **2.62×** |
| est. concurrent @ 25 fps | **~2 avatars** |
| first frame | 755 ms |
| VRAM idle/peak | 10407 / 10407 MiB |
| golden frames | 716 (deterministic) |

> Note: 65.6 fps (clean, isolated) is higher than the ~50 fps seen in production
> logs — production includes live-session contention + network. Use **65.6 fps** as
> the model-optimization baseline; the ~2-concurrent ceiling is unchanged (2.62×).

**Harness self-check:** FP32-vs-FP32 compare = SSIM 1.0000, PSNR ∞ → frames are
byte-identical and deterministic, so the SSIM≥0.98 / PSNR≥38 dB gate is a trustworthy
regression detector for Phases 1–2.

**Smoke status:**
- `smoke_ws_text.py` — GREEN (full turn, frames, `done`).
- `smoke_ws_interrupt.py` — **hangs by design, NOT a regression.** It sends
  `{type:'interrupt'}` and waits for `{type:'interrupted'}`, but the manual-interrupt
  path (`session.py:592` → `interrupt(send_event=False)`) cancels silently and does
  NOT echo `interrupted` (only *barge-in*, `send_event=True`, emits it; the frontend
  stops playback locally on a manual stop). For an interrupt regression gate use the
  barge-in path (`smoke_ws_realtime_barge.py`) or break the test on the next `done`.

**E2E latency baseline** (from `pipeline_done` logs): first_frame ~4.3 s,
client_first_render ~6.3 s, prebuffer ~2.2 s. (Unchanged by model optimizations.)

---

## Phase 1 — BF16 (lowest risk, do first)

**Goal:** ~1.3–1.8× fps and lower VRAM via `bfloat16` autocast on the UNet (and
optionally the audio encoder). Target: lift ~2 → ~3 avatars/GPU.

### 1.1 Implementation steps
1. Add env flag `SYNCTALK_DTYPE` (`fp32` default | `bf16`).
2. In `_gpu_forward` (L182–186), wrap the forward:
   ```python
   autocast_dtype = torch.bfloat16 if SYNCTALK_DTYPE == "bf16" else None
   with torch.no_grad():
       if autocast_dtype:
           with torch.autocast("cuda", dtype=autocast_dtype):
               preds = net(imgs, audio)
           preds = preds.float()          # CRITICAL: back to fp32 before *255
       else:
           preds = net(imgs, audio)
       preds_cpu = preds.cpu()
   ```
   > Use **autocast**, NOT `net.half()` — autocast keeps fp32 master weights and
   > casts per-op, which is safe; full `.to(bf16)` risks ops that don't support it.
3. Apply the **same autocast path to warmup** (L137–140) so CUDA kernels JIT in the
   right dtype (otherwise first real request pays a recompile stall).
4. (Optional) Wrap `audio_enc(mel...)` (L208) in the same autocast.

### 1.2 Validation
- Re-run Phase 0 quality compare (SSIM/PSNR vs golden). Must pass the threshold.
- Re-run throughput bench — record speedup and VRAM drop.
- Re-run latency baseline (first_frame should improve slightly).
- Smoke suite green.

### 1.3 Edge cases to test
- **Padded/short audio windows** (`_prep_batch` pads to `32*16*16`) — render a very
  short turn (1 word) and a long one (>200 chars); check no NaN/black frames.
- **`nearest_valid` fallback frames** — force a missing landmark index; confirm bf16
  path still composites.
- **Mixed-dtype guard** — assert `preds.dtype` is back to fp32 before `*255`
  (a bf16 array → `.astype(np.uint8)` silently corrupts values).
- **First-request-after-restart** — confirm warmup covered bf16, no cold stall.
- **Barge-in mid-turn** — `/infer_stream` abort still fires (`is_disconnected`).

### 1.4 Rollback & success
- Rollback: `SYNCTALK_DTYPE=fp32` + restart. Zero code revert needed.
- **Success:** ≥1.3× fps, SSIM ≥ 0.98, all smoke + edge tests green, latency not
  worse. Commit to SyncTalk repo, restart, re-measure live with `capture_ws_mp4`.

### 1.5 RESULTS — Phase 1 (2026-06-18): BF16 is a NO-OP here. Bottleneck found.

Implemented behind `SYNCTALK_DTYPE` (autocast in `_gpu_forward` + warmup,
`synctalk_server.py`). Restarted SyncTalk with `bf16`, benchmarked, then **reverted
to fp32** (flag kept in code, default fp32).

| Run | fps median | first frame | VRAM peak | quality vs golden |
|---|---|---|---|---|
| FP32 baseline | 65.6 | 755 ms | 10407 MiB | — |
| BF16 | 66.6 (**+1.5%, within noise**) | 696 ms | 12475 MiB | SSIM 0.9999, PSNR 60.9 dB (PASS) |

Quality is perfect, but **throughput barely moved**. A microbenchmark
(`SyncTalk_2D/bench_forward.py`) isolating the GPU forward explained why:

| Path | Throughput | Per-frame |
|---|---|---|
| GPU forward FP32 | **988 fps** | ~1.0 ms |
| GPU forward BF16 | 1068 fps (+8%) | ~0.94 ms |
| End-to-end `/infer_stream` | **~66 fps** | ~15 ms |

**The GPU forward is ~15× faster than the end-to-end pipeline.** ~93% of per-frame
time is NOT the GPU — it's the CPU `_composite` (full-frame `src_img.copy()` +
3× `cv2.resize` + `cv2.imencode` JPEG + base64, across `SYNCTALK_CPU_WORKERS=4`),
plus NDJSON/transport. **GPU precision is the wrong lever for this pipeline.**

### ⚠ ROADMAP REVISION (supersedes the original ordering)

- **Phase 2 (TensorRT): DEPRIORITIZED.** It optimizes the GPU forward, which is
  already 15× too fast. Expected end-to-end gain ≈ the BF16 gain (~noise). Do NOT
  invest the multi-day TensorRT effort for throughput. (It may still help VRAM /
  free SM time for cross-session batching — a secondary, smaller motive.)
- **NEW TOP PRIORITY — optimize the CPU frame path** (was buried in Phase 3/4):
  1. **Reduce `_composite` cost:** avoid the full-frame `.copy()` per frame (write
     into a reused buffer / only touch the mouth ROI); collapse the 3 resizes; move
     resize+composite to GPU (the frame is already on GPU pre-`.cpu()`).
  2. **Drop base64** on the SyncTalk→backend hop — send binary JPEG (backend already
     re-frames to binary WS; the b64 round-trip is pure waste).
  3. **NVENC (Phase 4 encode half):** replacing per-frame `cv2.imencode`+base64 with
     hardware H.264 removes the single biggest CPU cost AND the 10 Mbps bandwidth —
     this is now the highest-ROI item, not just a transport nicety.
  4. **More CPU workers / process pool** for `_composite` (quick test:
     bump `SYNCTALK_CPU_WORKERS` and re-run the harness).
- **Re-validate the bottleneck first:** before building, confirm composite-bound by
  bumping `SYNCTALK_CPU_WORKERS` (e.g. 4 → 12) and re-running `bench_synctalk.py`. If
  fps rises ~linearly, composite is confirmed as the ceiling.

### 1.6 RESULTS — server-side stage profile (2026-06-18)

Added per-stage timing behind `SYNCTALK_PROFILE=1` in `/infer_stream`'s `generate()`
(`synctalk_server.py`, logs prep/gpu/composite/serialize). Measured breakdown (avg of
6 runs of the 716-frame reference, ~14.8 ms/frame total, ~67 fps):

| Stage | ms/frame | share | what it is |
|---|---|---|---|
| **composite** | **7.2** | **49%** | full-frame `src_img.copy()` + 3× `cv2.resize` + `cv2.imencode` JPEG + base64 (CPU) |
| **gpu** (`_accum.infer`) | **5.4** | **37%** | pure forward ≈ 1 ms (microbench); the other ~4.4 ms is CPU↔GPU transfer (157 MB float32/batch) + the 15 ms `MAX_WAIT_S` batch-wait |
| prep (`_prep_batch`) | 1.5 | 10% | tensor stack/assembly |
| serialize (`json.dumps`) | 0.6 | 4% | NDJSON framing of the already-b64 frame |

Corrections to earlier guesses: **serialize/JSON is only 4% — NOT a bottleneck** (the
base64 cost lives *inside* composite, not in the json hop). The GPU *compute* is ~1 ms
(7%) → BF16/TensorRT confirmed irrelevant for throughput a third way.

#### Measurement-derived lever priority (this supersedes the 1.5 list)

1. **Pipeline the stages within a request (biggest structural win, ~2×).** Stages are
   currently **sequential** (sum = 14.8 ms/frame). If prep / gpu / composite / serialize
   overlapped (stage N+1 starts while N finishes), throughput would be gated by the
   *slowest* stage (composite ~7.2 ms) not the sum → ~67 → **~130 fps** (~5 avatars/GPU)
   with NO quality change. The backend already pipelines *across* chunks (2 workers); this
   is pipelining *within* a stream. Implement as a small producer/consumer chain in
   `generate()`. **Top priority.**
2. **Cut composite (the slowest stage, 7.2 ms).** Avoid the per-frame full-frame
   `.copy()` (write into a reused buffer / touch only the mouth ROI); collapse the 3
   resizes; move resize+composite to GPU (frame is on GPU pre-`.cpu()`). Raises the
   pipelined ceiling further.
3. **GPU-stage transfer (secondary, ~4.4 ms of the 5.4).** Use **pinned memory** +
   transfer **uint8** (¼ the bytes of float32) and normalize on GPU; drop/again-tune
   `MAX_WAIT_S` for the single-stream case. Cuts transfer without touching compute.
4. **NVENC + binary frames (Phase 4).** Replacing `cv2.imencode`+base64 inside composite
   with hardware H.264 removes a chunk of the composite cost AND the 10 Mbps bandwidth.
   Still the right long-term transport, now also a composite-cost win.

Note: the ~14.8 ms/frame stage-sum matches wall time → confirms stages run sequentially,
which is exactly why lever #1 (pipelining) is the highest-ROI, lowest-risk next step.

### 1.7 RESULTS — Stage pipelining SHIPPED (2026-06-18) ✅

Implemented in `/infer_stream` behind `SYNCTALK_PIPELINE` (default **on**;
`=0` → original sequential path). `prep` / `gpu` / `composite` now run as three
concurrent coroutines linked by bounded queues (`maxsize=2`); each stage is a single
in-order consumer so frame order is preserved. Abort-on-disconnect is handled in the
`gpu` stage plus a `finally` that cancels all worker tasks synchronously (cleanup is
guaranteed even when the generator is itself cancelled by a barge-in).

| Config | fps median | realtime | avatars/GPU | quality vs golden |
|---|---|---|---|---|
| Sequential (baseline) | 65.6 | 2.62× | ~2 | — |
| **Pipelined (shipped)** | **~105** | **4.2×** | **~4** | **SSIM 1.0000, PSNR ∞ (PASS)** |

**+60% throughput, ~2 → ~4 avatars/GPU, zero quality change** (byte-identical frames —
only scheduling changed). Server profile confirms the mechanism: wall (~6.4 s) ≈ the
slowest stage (composite) instead of the sum of all stages (~11.5 s).

Validation (all green):
- Quality: SSIM 1.0000 / PSNR ∞ vs the FP32 golden set.
- Full turn through the backend: `done`, frames flow, first_frame ~1.9 s (was ~4.3 s).
- **Abort-on-disconnect:** 5 abrupt mid-stream RST disconnects, then re-benchmark →
  105 fps holds, VRAM stable → workers are cancelled, no leak, barge-in intact.
- Rollback: `SYNCTALK_PIPELINE=0` + SyncTalk restart.

This is **live on the persistent SyncTalk server now** (default-on after the restart).

**Next bottleneck = composite (~8.7 ms/frame, now ~75% of wall).** Lever #2: cut the
per-frame full-frame `.copy()` + 3 resizes (reuse buffers / mouth-ROI only / GPU-side
composite). That raises the pipelined ceiling further toward ~6–8 avatars/GPU.

### 1.8 RESULTS — Lever #2: output-space composite (2026-06-18) ✅

Composite sub-step decomposition (isolated, 1920×1080 → 540×960):

| sub-step | ms | note |
|---|---|---|
| **JPEG `imencode`** | **1.83** | dominant (~59%); only NVENC removes it |
| copy 6.2 MB | 0.51 | per-frame full-res copy |
| resize FULL→540 (AREA) | 0.28 | full-frame downscale |
| resize crop→328 / 328→(w,h) | 0.21 / 0.17 | |
| b64 | 0.09 | |

Implemented `_composite_fast` behind `SYNCTALK_FAST_COMPOSITE` (precompute the 540×960
background + 328 border crop + output-space face coords once at load; per frame, resize
only the small face region into a 1.5 MB bg copy). Frees ~24 GB RAM potential (full-res
no longer needed by the fast path; currently both kept for A/B). Also learned:
**`cpu_workers` 4→10 gave nothing** — the pipeline's single `comp_stage` consumer
serialises composite, so the worker pool is underused (a future parallel-composite stage
with an ordering buffer is the structural follow-up).

| Config | fps median | realtime | avatars/GPU | quality vs golden |
|---|---|---|---|---|
| Baseline (sequential, full-res) | 65.6 | 2.62× | ~2 | — |
| + Pipelining | ~105 | 4.2× | ~4 | SSIM 1.0000 |
| **+ Fast composite** | **~130** | **5.2×** | **~5** | **SSIM 0.9993 (min 0.9980), PSNR 51 dB — PASS** |

**Total so far: 65.6 → ~130 fps (1.97×), ~2 → ~5 avatars/GPU.** Fast composite adds
+23% on top of pipelining (composite was the gating stage). Quality is now a tiny
approximation (not byte-identical) — SSIM 0.999 / PSNR 51 dB, visually indistinguishable.

Validation: full turn through backend OK (721 frames), 3 abrupt aborts → 131 fps holds
(abort/leak intact). Rollback: `SYNCTALK_FAST_COMPOSITE=0` + restart (byte-perfect path).

**DECISION: fast composite is the production default** (user-approved 2026-06-18 after a
visual worst-case comparison — diff confined to the mouth interior, imperceptible). Set
in `config.env` (`SYNCTALK_PIPELINE=1`, `SYNCTALK_FAST_COMPOSITE=1`) and passed through
`scripts/start_synctalk.sh`. Rollback: flip either flag to 0 in `config.env` + restart.

**Next bottleneck = JPEG `imencode` (1.83 ms, ~shared floor).** See Phase 4 findings.

### 1.9 RESULTS — Phase 4 feasibility (2026-06-18): NVENC infeasible; pivot to nvJPEG

Re-profiled under pipeline+fast: composite still gates (~7 ms/frame; isolated only ~2.9
ms — the gap is the single `comp_stage` consumer + GIL contention). JPEG `imencode` is
~1.83 ms of that. Investigated Phase 4 (NVENC H.264 + WebCodecs) and found:

| Approach | Bandwidth | Encode | Verdict |
|---|---|---|---|
| **NVENC h264** | ~2 Mbps | — | ❌ **H200 has NO NVENC** (Hopper data-center GPUs ship without the encoder block; `avcodec_open2("h264_nvenc")` fails) |
| **Software libx264** | 1.2–2.9 Mbps | **~15 ms/f** | ❌ 8× slower than JPEG → would cut fps ~130→~60; SSIM 0.95–0.97 (below gate) |
| **GPU nvJPEG** (torchvision, H200 JPEG engine) | JPEG (12.7 Mbps) | **0.26 ms/f** | ✅ 7× faster than cv2.imencode; dedicated JPEG silicon (no SM cost); **no frontend change** |

**Bandwidth reality check:** JPEG = 12.73 Mbps/stream; 5 streams = 63.7 Mbps < ~88 Mbps
uplink → **fits**. Bandwidth only binds past ~6–7 avatars (≈ where the GPU caps). The
earlier "50 Mbps tight" used a stale 10 Mbps estimate. So the bandwidth-motivated H.264
transport rewrite is **not warranted on this hardware now**, and NVENC can't do it anyway.

**Revised conclusion:** the real remaining lever on this box is **GPU JPEG (nvJPEG)** —
move `imencode` (and ideally the composite blend) onto the GPU, keeping JPEG and the
entire existing frontend/transport. H.264/WebCodecs is deferred until/unless we scale to
many GPUs or need mobile-grade bandwidth robustness.

### 1.10 RESULTS — GPU nvJPEG: NET LOSS in-pipeline (2026-06-18)

Implemented behind `SYNCTALK_GPU_JPEG` (batch the composited frames → GPU → nvJPEG →
b64). Despite 0.26 ms/f in isolation, **in the pipeline it was slower: 83 fps vs ~130**
(and SSIM 0.9917 vs golden — nvJPEG ≠ libjpeg-turbo). Why:
- the CPU composite → GPU round-trip per batch (transfer 54 MB up) plus
- nvJPEG returns N variable-length byte buffers → ~36 individual device→host `.cpu()`
  syncs per batch, each stalling, and
- the nvJPEG work serialises against the model forward on the GPU.

**nvJPEG only pays off if the composite itself runs on the GPU** (frames already
resident, no round-trip). With CPU composite it's a net loss. Reverted; flag kept off.
Shipped config remains **pipeline + fast composite (CPU cv2.imencode) ≈ ~5 avatars/GPU**.

### Status summary — optimization campaign
| Lever | Outcome |
|---|---|
| BF16 / TensorRT | ❌ no-op (GPU forward already 15× faster than the pipeline) |
| **Stage pipelining** | ✅ shipped — ~2 → ~4 avatars, byte-identical |
| **Fast (output-space) composite** | ✅ shipped — ~4 → ~5 avatars, SSIM 0.999 |
| CPU JPEG (cv2) | kept — fastest in-pipeline encode found |
| NVENC H.264 | ❌ infeasible (no H200 encoder) |
| Software libx264 | ❌ ~15 ms/f, would tank fps; bandwidth not binding |
| GPU nvJPEG (alone) | ❌ net loss in-pipeline (round-trip + D2H syncs) |

**Net result: ~2 → ~5 concurrent avatars/GPU (≈2×), quality SSIM 0.999.** Remaining
options if more single-GPU capacity is needed: **GPU compositing + nvJPEG** (the only way
nvJPEG helps; bigger rewrite), or **parallel CPU composite** (modest). Otherwise this is a
solid stopping point.

Files changed (SyncTalk repo — commit there): `synctalk_server.py` (`SYNCTALK_DTYPE`,
`SYNCTALK_PROFILE`, `SYNCTALK_PIPELINE` + pipelined `/infer_stream`), new `bench_forward.py`.
(avatar-system-2 repo: `scripts/bench_synctalk.py`, `scripts/start_synctalk.sh`,
`OPTIMIZATION_PLAN.md`.)

---

## Phase 2 — TensorRT (biggest model win; ONNX export already exists)

**Goal:** 2 → ~4 avatars/GPU via a fused FP16 TRT engine for the UNet.

### 2.1 Prerequisites
- Confirm `tensorrt` + (recommended) `torch-tensorrt` installed in the
  `synctalk2d` conda env, versions matching the CUDA/driver. If absent, install
  pinned versions.
- **Per-checkpoint engines:** a TRT engine bakes in the weights AND is GPU-specific.
  → one engine per `(avatar checkpoint, GPU model)`. Plan a build script, not a
  one-off.

### 2.2 Build steps
1. **ONNX export with dynamic batch.** Adapt the existing `unet_328.py` export:
   ```python
   torch.onnx.export(net, (img, audio), "unet.onnx",
       input_names=["input","audio"], output_names=["output"],
       dynamic_axes={"input":{0:"B"}, "audio":{0:"B"}, "output":{0:"B"}},
       opset_version=17)
   ```
   Validate with the existing `check_onnx` (ONNXRuntime) — must match torch output.
2. **Build the engine** covering the batch range (`BATCH_SIZE=32`,
   `FrameAccumulator.MAX_FRAMES=128`):
   ```bash
   trtexec --onnx=unet.onnx --fp16 \
     --minShapes=input:1x6x320x320,audio:1x32x16x16 \
     --optShapes=input:32x6x320x320,audio:32x32x16x16 \
     --maxShapes=input:128x6x320x320,audio:128x32x16x16 \
     --saveEngine=checkpoint/<avatar>/unet_h200_fp16.plan
   ```
3. **Integration (recommended: torch-tensorrt for a drop-in callable):**
   ```python
   compiled = torch_tensorrt.compile(net, inputs=[...min/opt/max...],
                                     enabled_precisions={torch.float16})
   ```
   Call `compiled(imgs, audio)` in `_gpu_forward` behind `SYNCTALK_BACKEND=trt|torch`
   (fallback to eager `net`). Keep the same tensor interface so the rest is untouched.
   - Alternative (manual TRT runtime + bindings): more code, slightly faster/more
     control. Only if torch-tensorrt underperforms.

### 2.3 Validation
- **Dynamic-batch correctness:** run inference at batch 1, 32, 64, 100, 128 — TRT
  must serve all without re-fit; compare each against golden frames (SSIM/PSNR).
- Throughput bench + VRAM. Expect ~2–3× over FP32 eager.
- Latency baseline + full smoke suite.

### 2.4 Edge cases to test
- **Batch above maxShapes** — `FrameAccumulator` could in theory exceed 128 if
  `MAX_FRAMES` is raised; assert the server clamps or rebuild with a higher max.
  Add a guard: if batch > engine max → fall back to eager (`net`) for that batch.
- **First batch after restart** — engine deserialize + warmup must run at startup,
  not on first request.
- **Checkpoint swap** — switching `SYNCTALK_AVATAR` to a checkpoint with **no built
  engine** must fall back to eager (or fail loudly), not load a mismatched engine.
- **FP16 precision on the face** — TRT FP16 is more aggressive than autocast bf16;
  re-check lip/teeth artifacts carefully. If borderline, try mixed precision
  (`--precisionConstraints` to keep sensitive layers fp32).
- **Numerical NaN under padded audio** — same short/long-turn tests as Phase 1.
- **Barge-in abort** still works (engine call is inside the same batch loop).
- **GPU memory fragmentation** — engine + torch context coexist; verify VRAM under
  sustained load doesn't creep (run 100 turns, watch `nvidia-smi`).

### 2.5 Rollback & success
- Rollback: `SYNCTALK_BACKEND=torch` + restart.
- **Success:** ≥2× fps over FP32, quality threshold met at all batch sizes, smoke +
  edge tests green. Document the build command per checkpoint in DEPLOY.md.

---

## Phase 3 — FAQ video cache (take common answers off the GPU)

**Goal:** serve top FAQ / repeated answers as **pre-rendered MP4** (zero GPU, near-
zero latency), reusing the intro-video infrastructure. Frees the GPU for the long
tail → effectively raises concurrency for FAQ-heavy traffic.

### 3.1 Scoping
- Pull `winner_source` distribution from `pipeline_done` logs → estimate the % of
  traffic that is FAQ / cache-hit (deterministic, cacheable). Only worth it if
  non-trivial.
- Only **static** answers qualify (FAQ fast-path, fixed semantic-cache entries).
  Dynamic RAG/Gemini answers cannot be video-cached.

### 3.2 Build steps
1. **Cache key:** `(avatar, voice, language, sha256(normalized_spoken_text))`.
   Path: `cache/faq/video/<avatar>/<hash>.mp4` (gitignored, like intro cache).
2. **Offline renderer** (`scripts/build_faq_videos.py`, new): for each FAQ entry ×
   language → Soniox TTS → SyncTalk `/infer` (batch render, non-realtime) → frames
   → ffmpeg H.264+AAC MP4 (reuse `intro.py` ffmpeg path / `_ffmpeg_bin()`).
3. **Lazy fill:** on a cache miss for a static answer, render in the background and
   cache for next time (don't block the live turn — serve realtime this once).
4. **Serve path:** in `session.py run_query`, when the race winner is FAQ/cache **and**
   a valid MP4 exists → send `{type:'faq_video', url}` (mirror `intro_video`) and skip
   the realtime pipeline. Frontend plays it through the same `<video>` element path
   used for the intro.

### 3.3 Validation
- Cache hit → correct clip, correct language, lip-sync intact (it's pre-rendered, so
  exact).
- Cache miss → falls back to realtime, then the clip exists on the next ask.
- A/V sync of the cached MP4 matches a freshly rendered realtime turn.

### 3.4 Edge cases to test
- **Barge-in during a cached clip** — must stop like the intro (reuse intro stop /
  interrupt control); confirm a new query starts cleanly.
- **Stale cache** — change a FAQ answer's text → key changes → new render; old clip
  ignored. Test the invalidation explicitly.
- **Checkpoint / voice change** — key includes both → no cross-avatar/voice clip
  served. Switch `SYNCTALK_AVATAR` and confirm no stale clip plays.
- **Language mismatch** — RU query must not get an EN clip; key includes language.
- **Missing/corrupt MP4** — `intro_video_is_valid`-style guard → fall back to
  realtime, never send a broken URL.
- **Partial render / interrupted build** — write to a temp file, atomic rename
  (reuse the `_INTRO_VIDEO_BUILD_LOCK` pattern from `intro.py` to avoid concurrent
  double-builds).
- **Session gate interaction** — a cached-video turn should ideally NOT hold the GPU
  pipeline slot (it doesn't touch SyncTalk) — decide whether it counts toward the
  gate; document it.

### 3.5 Rollback & success
- Rollback: feature flag `FAQ_VIDEO_CACHE=0` → always realtime.
- **Success:** measured drop in GPU turns for FAQ traffic, sub-second response for
  cache hits, all edge cases green.

---

## Phase 4 — NVENC + WebCodecs transport (bandwidth/quality; biggest rework)

**Goal:** replace per-frame JPEG-over-WS (~10 Mbps) with hardware-encoded H.264
(~1–2 Mbps), decoded via **WebCodecs** on the client. Wins: 5–10× less bandwidth,
cleaner image, robust on mobile, enables multi-session over the tunnel; modest
latency gain (trim prebuffer ~0.5–1 s on good networks, large gain on poor ones).

> Latency caveat: the dominant latency is STT→LLM→TTS→render (first_frame ~4.3 s).
> Transport only affects the prebuffer/network slice. Don't expect this to fix the
> *pipeline* latency — it fixes bandwidth, quality, and starvation risk.

### 4.1 Why WebCodecs over MSE
- MSE buffers more (designed for files/long segments) → higher latency, fiddly with
  short interrupt-heavy turns.
- **WebCodecs** (`VideoDecoder`) gives frame-level control and lower latency, fits
  the existing per-frame canvas model better. Recommend it. Keep MSE only as a
  fallback for browsers without WebCodecs.

### 4.2 Build steps (SyncTalk side)
1. Add **NVENC** encode in `synctalk_server.py`: instead of `_composite`'s
   `cv2.imencode(".jpg")` + base64, feed the composited 540×960 BGR frame to an
   NVENC H.264 encoder (e.g. `PyNvVideoCodec`/VALI, or `ffmpeg h264_nvenc` via pipe).
   - Use **low-latency config**: short GOP or all-intra, `tune=ll`, no B-frames.
   - Emit an **init segment** (SPS/PPS) per turn + encoded NAL units as the stream.
2. New env flag `SYNCTALK_TRANSPORT=jpeg|h264`. Keep JPEG path as fallback.

### 4.3 Build steps (backend side)
- `ws_writer.py`: new frame type for H.264 NAL chunks (extend the `0xF1` binary
  framing with a codec tag, e.g. `0xF2 = h264`). Forward NAL units + the per-turn
  init segment.

### 4.4 Build steps (frontend side)
- `useChunkPlayback.ts`: branch on codec tag. For H.264, feed NAL units into a
  `VideoDecoder` (WebCodecs), render decoded `VideoFrame`s to the same canvas on the
  existing render-loop timeline. Keep the gapless-audio scheduler unchanged (audio
  still on AudioContext) OR mux audio into the stream later.
- Reduce `LIVE_PREBUFFER_S` cautiously (e.g. 2.2 → 1.5) and re-measure starvation.

### 4.5 Validation
- Bandwidth: measure bytes/turn vs JPEG baseline (expect 5–10× less).
- Quality: SSIM/PSNR of decoded H.264 frames vs JPEG frames on the same turn.
- Latency: first_frame, client_first_render, and the starvation margin
  (`client_first_render − first_frame`) vs baseline.
- Full smoke suite.

### 4.6 Edge cases to test
- **Interrupt mid-stream** — decoder must reset cleanly between turns; a new turn's
  init segment must re-key the decoder (no green/garbled frames from stale ref
  frames).
- **Keyframe loss** — if the opening keyframe is dropped, decoder can't start;
  ensure each turn begins with an IDR and the init segment is delivered reliably.
- **WebCodecs unsupported** (older Safari/Firefox) — fall back to MSE or JPEG path;
  detect at runtime.
- **A/V drift** — without muxed audio, verify lip-sync over a long turn (the canvas
  timeline vs AudioContext cursor); WebCodecs frame timestamps must align to the
  existing audio cursor.
- **Mobile / constrained network** — test on a throttled 3 Mbps connection; this is
  where H.264 should clearly beat JPEG (which would stutter).
- **NVENC session limits** — consumer drivers cap concurrent NVENC sessions; on
  datacenter H200 this is unlocked, but verify under multi-session.
- **Cloudflare Tunnel** — confirm binary H.264 frames traverse the tunnel fine
  (they will; smaller than JPEG) and re-check the throughput tell in `pipeline_done`.
- **Barge-in + cached FAQ video (Phase 3)** — ensure the two video paths
  (`faq_video` MP4 vs live H.264 stream) don't collide in the frontend player state.

### 4.7 Rollback & success
- Rollback: `SYNCTALK_TRANSPORT=jpeg` + frontend codec branch off → back to JPEG.
- **Success:** ≥5× bandwidth reduction, quality ≥ JPEG, smooth on a throttled
  connection, prebuffer trimmed without starvation, all edge cases green.

---

## Cross-cutting concerns

### Testing strategy (applies to every phase)
1. **Unit/quality:** SSIM/PSNR vs golden frames (Phase 0).
2. **Smoke:** the four `scripts/smoke_*` / `capture_*` tests — must stay green.
3. **Edge cases:** the per-phase lists above.
4. **Soak test:** 100–200 back-to-back turns with random barge-ins; watch VRAM
   creep, fps stability, and that `[infer_stream] aborted on disconnect` still fires.
5. **Live A/B:** flag on vs off, compare `pipeline_done` metrics over real turns.

### Edge cases that span phases (regression watchlist)
- **Barge-in / interrupt** must keep working after every change — it's the most
  fragile path (`cancel_all`, `is_disconnected`, non-blocking `interrupt()`).
- **Multi-segment head-pose continuity** (`start_frame` / ping-pong) must not break —
  any change to `_gpu_forward`/`_encode_audio`/batching can resnap the head.
- **FrameAccumulator cross-session batching** — when the gate is raised >1, verify
  frames from two sessions in one GPU pass don't bleed (offset slicing in
  `_do_flush`).
- **Quality regression** is the silent killer — never merge a phase whose SSIM falls
  below threshold, even if it's faster.

### Raising the session gate (the payoff)
After Phase 1/2 lift fps, raise `MAX_CONCURRENT_SESSIONS` (in `session_gate.py`)
conservatively: 1 → 2, soak-test two simultaneous talking sessions, watch for
sub-25-fps drops. Only then consider 3+. Also parallelize the CPU `_composite`
(currently `_cpu_exec`, 4 workers) — it becomes the next bottleneck once the GPU is
faster and multiple sessions composite concurrently.

### Monitoring
- Keep `pipeline_done` metrics as the source of truth: `first_frame`,
  `client_first_render`, `total`, and the starvation margin.
- Add per-phase counters: GPU batch ms (Phase 1/2), cache hit rate (Phase 3),
  bytes/turn (Phase 4).

---

## Appendix A — env flags introduced

| Flag | Phase | Default | Purpose |
|---|---|---|---|
| `SYNCTALK_DTYPE` | 1 | `fp32` | `bf16` enables autocast |
| `SYNCTALK_BACKEND` | 2 | `torch` | `trt` runs the TensorRT engine |
| `FAQ_VIDEO_CACHE` | 3 | `0` | serve pre-rendered FAQ MP4s |
| `SYNCTALK_TRANSPORT` | 4 | `jpeg` | `h264` enables NVENC + WebCodecs |

## Appendix B — capacity & cost math

- 1 GPU ≈ 2 realtime avatars today; ~3 after BF16; ~4 after TensorRT.
- 10 concurrent avatars: 5× H200 baseline → 3× H200 optimized (~−40% GPU).
- Biggest cost lever is **autoscaling + demo-hours-only** (up to −75%), then model
  optimization (−40%). Per-session API cost (Soniox STT/TTS + Gemini) ≈ $0.3–0.6 per
  avatar-hour — small vs GPU. (All figures are estimates; confirm with a benchmark
  and a current cloud-GPU quote.)

## Appendix C — recommended sequence & effort

| Order | Phase | Effort | Risk | Payoff |
|---|---|---|---|---|
| 1 | BF16 | ~1 day | low | 2 → ~3 /GPU |
| 2 | TensorRT | days | medium | 2 → ~4 /GPU |
| 3 | FAQ video cache | medium | low | FAQ traffic off-GPU |
| 4 | NVENC + WebCodecs | weeks | high | −80% bandwidth, quality, multi-session |

Do not skip Phase 0 — without the benchmark harness and golden frames, you can't
prove any phase is a net win.
