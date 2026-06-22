import type { VercelRequest, VercelResponse } from "@vercel/node";

// Mint a LiveAvatar LITE session and start it — both server-side so LIVEAVATAR_API_KEY never
// reaches the browser. Returns the LiveKit creds (video) + ws_url (audio commands) the client needs.
// Ref: liveavatar-integrate skill → lite-mode-guide.md.
const SANDBOX_AVATAR_ID = "dd73ea75-1218-4ef3-92ce-606d5f7fbc0a"; // LITE sandbox (no credits)

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "POST only" });
    return;
  }
  const apiKey = process.env.LIVEAVATAR_API_KEY;
  if (!apiKey) {
    res.status(500).json({ error: "LIVEAVATAR_API_KEY is not set on the server" });
    return;
  }
  const base = process.env.LIVEAVATAR_API_BASE || "https://api.liveavatar.com";
  const sandbox = process.env.LIVEAVATAR_SANDBOX === "1";
  const avatarId = sandbox ? SANDBOX_AVATAR_ID : process.env.LIVEAVATAR_AVATAR_ID;
  if (!avatarId) {
    res.status(500).json({ error: "LIVEAVATAR_AVATAR_ID is not set (or set LIVEAVATAR_SANDBOX=1)" });
    return;
  }

  try {
    // 1) Session token (LITE).
    const tokenResp = await fetch(`${base}/v1/sessions/token`, {
      method: "POST",
      headers: { "X-API-KEY": apiKey, "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "LITE", avatar_id: avatarId, ...(sandbox ? { is_sandbox: true } : {}) }),
    });
    const tokenBody: any = await tokenResp.json().catch(() => ({}));
    const sessionToken = tokenBody?.data?.session_token;
    const sessionId = tokenBody?.data?.session_id;
    if (!sessionToken) {
      res.status(502).json({ error: "no session_token from LiveAvatar", detail: tokenBody });
      return;
    }

    // 2) Start the session → LiveKit creds (video) + ws_url (audio commands).
    const startResp = await fetch(`${base}/v1/sessions/start`, {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
    });
    const startBody: any = await startResp.json().catch(() => ({}));
    const d = startBody?.data || {};
    if (!d.livekit_url || !d.ws_url) {
      res.status(502).json({ error: "session start did not return livekit_url / ws_url", detail: startBody });
      return;
    }

    res.status(200).json({
      sessionId,
      // session_token is returned so the client can DELETE /v1/sessions on teardown.
      sessionToken,
      livekitUrl: d.livekit_url,
      livekitToken: d.livekit_client_token,
      wsUrl: d.ws_url,
      sandbox,
    });
  } catch (e) {
    res.status(502).json({ error: "LiveAvatar session setup failed", detail: String(e) });
  }
}
