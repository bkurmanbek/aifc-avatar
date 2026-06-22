import type { VercelRequest, VercelResponse } from "@vercel/node";
import { GoogleGenAI } from "@google/genai";

// Google Gemini TTS (voice "Sulafat") → raw PCM 16-bit / 24 kHz / mono, base64-encoded — exactly
// the format LiveAvatar LITE `agent.speak` wants (no resampling). Key stays server-side.
export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "POST only" });
    return;
  }
  const text = String((req.body && (req.body as any).text) || "").trim();
  if (!text) {
    res.status(400).json({ error: "text is required" });
    return;
  }
  const apiKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
  if (!apiKey) {
    res.status(500).json({ error: "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set" });
    return;
  }
  const model = process.env.GEMINI_TTS_MODEL || "gemini-3.1-flash-tts-preview";
  const voice = process.env.GEMINI_TTS_VOICE || "Sulafat";
  const style = (process.env.GEMINI_TTS_STYLE || "").trim().replace(/:\s*$/, "");

  try {
    const ai = new GoogleGenAI({ apiKey });
    const resp: any = await ai.models.generateContent({
      model,
      contents: style ? `${style}: ${text}` : text,
      config: {
        responseModalities: ["AUDIO"],
        speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: voice } } },
      },
    });
    // Gemini returns base64 PCM (24 kHz) inline; concat DECODED bytes (base64 concat is unsafe).
    const buffers: Buffer[] = [];
    for (const c of resp?.candidates || []) {
      for (const p of c?.content?.parts || []) {
        const data = p?.inlineData?.data;
        if (data) buffers.push(Buffer.from(data, "base64"));
      }
    }
    const pcm = Buffer.concat(buffers);
    if (pcm.length === 0) {
      res.status(502).json({ error: "Gemini TTS returned no audio", model, voice });
      return;
    }
    res.status(200).json({ audioBase64: pcm.toString("base64"), sampleRate: 24000, voice, model });
  } catch (e) {
    res.status(502).json({ error: "Gemini TTS failed", detail: String(e) });
  }
}
