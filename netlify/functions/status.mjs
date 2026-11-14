// Netlify Function (v2): GET /status answers 200 while the bot is running and 503 when it has gone quiet, for an uptime monitor.
// It reads status.json from the site's own static files; `ledgerbell heartbeat` writes that file and a deploy publishes it.
import { evaluate } from "../lib/evaluate.mjs";

export const config = { path: "/status" };

export default async (req) => {
  let status = null;
  try {
    const res = await fetch(new URL("/status.json", req.url), { headers: { "cache-control": "no-cache" } });
    if (res.ok) status = await res.json();
  } catch {
    status = null;
  }
  const { code, body } = evaluate(status, Date.now());
  return new Response(JSON.stringify(body), {
    status: code,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
};
