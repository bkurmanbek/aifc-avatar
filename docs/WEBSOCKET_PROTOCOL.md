# WebSocket Protocol

Endpoint: `GET /ws`

## Client To Server

- `prepare_stt`: preconnect realtime STT.
- `audio_chunk`: base64 PCM sixteen kilohertz mono frame.
- `audio`: base64 final WAV/audio payload.
- `text`: text prompt.
- `interrupt`: cancel active response.
- `reset`: reset conversation state and close STT.
- `close_stt`: close realtime STT.
- `client_first_render`: report first rendered chunk for latency metrics.
- `client_log`: frontend/browser log event.

## Server To Client

- `session_state`: WebSocket session ID and state.
- `partial`: live user transcript text.
- `transcript`: accepted final user transcript.
- `transcript_empty`: speech was empty, duplicate, or rejected.
- `stt_ready`: realtime STT is connected.
- `response_start`: assistant turn started; includes `turn_id`.
- `policy_state`: answer language/policy state.
- `response_chunk`: assistant text delta or sentence chunk.
- `answer_payload`: structured answer details and follow-ups.
- `audio_ready`: base64 WAV chunk ready for playback.
- `frame_cache`: URL for cached intro avatar frames.
- `frame`: base64 JPEG avatar frame.
- `chunk_done`: avatar frame stream ended for a chunk.
- `media_error`: TTS or SyncTalk failed for a chunk.
- `status`: user-visible status.
- `interrupted`: active turn was cancelled.
- `stop_confirmed`: stop command was accepted.
- `done`: turn completed; includes chunk count and latency metrics.
- `error`: recoverable session or pipeline error.

All turn-bound assistant/media messages should include `turn_id`. The frontend treats unmatched `turn_id` messages as stale.

