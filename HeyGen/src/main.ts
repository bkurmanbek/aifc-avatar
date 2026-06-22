import { Room, RoomEvent, Track, type RemoteTrack } from "livekit-client";

// LiveAvatar LITE (modular) client: LiveKit room → avatar video/audio; WebSocket(ws_url) → our
// Google Sulafat TTS audio via agent.speak. Protocol per HeyGen lite-mode-guide.md.

const $ = (id: string) => document.getElementById(id) as HTMLElement;
const video = $("avatar") as HTMLVideoElement;
const statusEl = $("status");
const input = $("text") as HTMLInputElement;
const startBtn = $("start") as HTMLButtonElement;
const speakBtn = $("speak") as HTMLButtonElement;
const stopBtn = $("stop") as HTMLButtonElement;

// 24 kHz × 16-bit × mono = 48,000 bytes/sec. First chunk 600 ms (buffer), then 1 s chunks.
const BYTES_PER_SEC = 48_000;
const FIRST_CHUNK = Math.floor(BYTES_PER_SEC * 0.6);
const NEXT_CHUNK = BYTES_PER_SEC;

let room: Room | null = null;
let ws: WebSocket | null = null;
let wsConnected = false;
let sending = false;
let keepAlive: number | undefined;

const setStatus = (s: string) => (statusEl.textContent = s);

async function start() {
  startBtn.disabled = true;
  setStatus("creating LiveAvatar session…");
  let s: any;
  try {
    const r = await fetch("/api/session", { method: "POST" });
    s = await r.json();
    if (!r.ok) throw new Error(JSON.stringify(s));
  } catch (e) {
    setStatus("session error: " + String(e));
    startBtn.disabled = false;
    return;
  }

  // 1) Video (+ avatar audio) via LiveKit. start() is a user gesture → audio may autoplay.
  room = new Room();
  room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
    if (track.kind === Track.Kind.Video) track.attach(video);
    else if (track.kind === Track.Kind.Audio) track.attach(); // appended <audio>, autoplays
  });
  try {
    await room.connect(s.livekitUrl, s.livekitToken);
  } catch (e) {
    setStatus("livekit connect failed: " + String(e));
    return;
  }
  setStatus("video connected — opening audio command channel…");

  // 2) Audio command channel (WebSocket). Must wait for state "connected" before sending.
  ws = new WebSocket(s.wsUrl);
  ws.onmessage = (ev) => {
    let m: any;
    try { m = JSON.parse(ev.data); } catch { return; }
    switch (m.type) {
      case "session.state_updated":
        wsConnected = m.state === "connected";
        if (wsConnected) {
          setStatus("ready — type something and Speak");
          speakBtn.disabled = false;
          stopBtn.disabled = false;
          input.disabled = false;
        } else setStatus("session: " + m.state);
        break;
      case "agent.speak_started": setStatus("speaking…"); break;
      case "agent.speak_ended": setStatus("ready"); break;
    }
  };
  ws.onclose = () => { wsConnected = false; setStatus("audio channel closed"); };
  ws.onerror = () => setStatus("audio channel error");

  keepAlive = window.setInterval(() => {
    if (ws && wsConnected) ws.send(JSON.stringify({ type: "session.keep_alive", event_id: `ka-${Date.now()}` }));
  }, 120_000);
}

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
function bytesToB64(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

async function speak(text: string) {
  if (!ws || !wsConnected) { setStatus("not connected"); return; }
  setStatus("synthesizing (Sulafat)…");
  let s: any;
  try {
    const r = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    s = await r.json();
    if (!r.ok) throw new Error(JSON.stringify(s));
  } catch (e) {
    setStatus("tts error: " + String(e));
    return;
  }

  const pcm = b64ToBytes(s.audioBase64); // raw PCM 24 kHz, exactly what agent.speak wants
  const eventId = `speak-${Date.now()}`;  // ONE id for the whole utterance
  sending = true;
  let off = 0, first = true;
  while (off < pcm.length && sending) {
    const size = first ? FIRST_CHUNK : NEXT_CHUNK;
    const chunk = pcm.subarray(off, off + size);
    off += chunk.length;
    first = false;
    ws.send(JSON.stringify({ type: "agent.speak", event_id: eventId, audio: bytesToB64(chunk) }));
  }
  if (sending) ws.send(JSON.stringify({ type: "agent.speak_end", event_id: eventId }));
  sending = false;
}

function interrupt() {
  sending = false; // stop the send loop FIRST, else queued chunks play after the interrupt
  if (ws && wsConnected) ws.send(JSON.stringify({ type: "agent.interrupt" }));
  setStatus("interrupted");
}

startBtn.onclick = () => void start();
speakBtn.onclick = () => { const t = input.value.trim(); if (t) void speak(t); };
stopBtn.onclick = () => interrupt();
input.addEventListener("keydown", (e) => { if (e.key === "Enter") speakBtn.click(); });

window.addEventListener("beforeunload", () => {
  try { if (keepAlive) clearInterval(keepAlive); ws?.close(); room?.disconnect(); } catch {}
});
