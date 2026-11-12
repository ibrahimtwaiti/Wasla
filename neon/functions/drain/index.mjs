// Neon Function Trigger worker: no public unauthenticated processing.
// This function schedules the protected Wasla Python worker on Vercel.
// Secrets are injected through Neon Functions environment settings.
export default {
  async fetch(request) {
    if (request.method !== "POST") return new Response("method not allowed", { status: 405 });
    if (!request.headers.get("x-neon-trigger-invocation-id")) {
      return new Response("scheduled triggers only", { status: 403 });
    }
    const url = process.env.WASLA_WORKER_URL;
    const secret = process.env.WASLA_WORKER_SECRET;
    const bypass = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
    if (!url || !url.startsWith("https://") || !secret || secret.length < 32 || !bypass) {
      return new Response("worker configuration incomplete", { status: 503 });
    }
    try {
      const result = await fetch(url, {
        method: "POST",
        headers: {
          "authorization": "Bearer " + secret,
          "x-vercel-protection-bypass": bypass,
          "content-type": "application/json",
        },
        body: "{}",
        signal: AbortSignal.timeout(25000),
      });
      if (!result.ok) return new Response("worker returned " + result.status, { status: 502 });
      // The application holds the row lock and handles retries/idempotency.
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    } catch (error) {
      return new Response("worker unavailable; pending events stay in database", { status: 502 });
    }
  },
};
