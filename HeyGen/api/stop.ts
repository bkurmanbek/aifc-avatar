import type { VercelRequest, VercelResponse } from "@vercel/node";

// Stop a LiveAvatar session immediately so it stops burning credits (otherwise it lingers until the
// 5-min idle timeout). Correct endpoint is POST /v1/sessions/stop with the session_token as Bearer
// (the guide's DELETE /v1/sessions returns 405). Called from the browser on unload via sendBeacon.
export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "POST only" });
    return;
  }
  let token = "";
  try {
    const b = typeof req.body === "string" ? JSON.parse(req.body) : req.body;
    token = (b && (b as any).sessionToken) || "";
  } catch { /* ignore */ }
  if (!token) {
    res.status(400).json({ error: "sessionToken required" });
    return;
  }
  const base = process.env.LIVEAVATAR_API_BASE || "https://api.liveavatar.com";
  try {
    const r = await fetch(`${base}/v1/sessions/stop`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
    res.status(r.ok ? 200 : 502).json({ stopped: r.ok });
  } catch (e) {
    res.status(502).json({ error: "stop failed", detail: String(e) });
  }
}
