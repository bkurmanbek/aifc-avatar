from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TurnMetrics:
    started_at: float
    mode: str
    stt_started_at: float | None = None
    stt_done_at: float | None = None
    plan_done_at: float | None = None
    llm_done_at: float | None = None
    spoken_ready_at: float | None = None
    postprocess_done_at: float | None = None
    payload_done_at: float | None = None
    first_audio_at: float | None = None
    first_frame_at: float | None = None
    client_first_render_at: float | None = None
    done_at: float | None = None
    race_timings: dict[str, object] = field(default_factory=dict)

    def as_ms(self) -> dict[str, int]:
        def delta(point: float | None) -> int | None:
            if point is None:
                return None
            return int((point - self.started_at) * 1000)

        payload = {
            "stt": None if self.stt_started_at is None or self.stt_done_at is None else int((self.stt_done_at - self.stt_started_at) * 1000),
            "plan_retrieve": delta(self.plan_done_at),
            "llm_generate": None if self.plan_done_at is None or self.llm_done_at is None else int((self.llm_done_at - self.plan_done_at) * 1000),
            "spoken_ready": delta(self.spoken_ready_at),
            "spoken_postprocess": None if self.llm_done_at is None or self.postprocess_done_at is None else int((self.postprocess_done_at - self.llm_done_at) * 1000),
            "payload_ready": delta(self.payload_done_at),
            "first_audio": delta(self.first_audio_at),
            "first_frame": delta(self.first_frame_at),
            "client_first_render": delta(self.client_first_render_at),
            "total": delta(self.done_at),
        }
        for key, value in self.race_timings.items():
            if isinstance(value, (int, float, str)):
                payload[key] = value
        return {key: value for key, value in payload.items() if value is not None}
