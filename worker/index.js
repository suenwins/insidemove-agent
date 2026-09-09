/**
 * InsideMove Agent — Telegram webhook receiver.
 *
 * Telegram gives a webhook a few seconds before it retries, and a button that
 * spins for a minute feels broken. GitHub Actions takes 20-40 seconds just to
 * start a runner. So this Worker sits between them and does three things in
 * under 50ms:
 *
 *   1. verifies the request really came from Telegram (secret header)
 *   2. answers the callback query, so the button stops spinning immediately
 *   3. fires a repository_dispatch and returns 200
 *
 * The Actions job then does the slow work and sends the note as a new message.
 *
 * Secrets (wrangler secret put <NAME>):
 *   TELEGRAM_BOT_TOKEN     from @BotFather
 *   TELEGRAM_SECRET        any random string; also given to setWebhook
 *   TELEGRAM_CHAT_ID       your chat id — everything else is ignored
 *   GITHUB_TOKEN           fine-grained PAT, Contents: read/write on this repo
 *   GITHUB_REPO            "owner/insidemove-agent"
 */

const ACK = {
  approve: "Researching…",
  ptc: "Building chart…",
  dismiss: "Dismissed",
};

function ackFor(data) {
  const verb = String(data || "").split(":")[0];
  return { a: ACK.approve, p: ACK.ptc, d: ACK.dismiss }[verb] || "Working…";
}

async function telegram(env, method, body) {
  return fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function dispatch(env, update, chatId) {
  const res = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
    {
      method: "POST",
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        "content-type": "application/json",
        "user-agent": "insidemove-agent-worker",
        "x-github-api-version": "2022-11-28",
      },
      body: JSON.stringify({
        event_type: "telegram_update",
        client_payload: { update, chat_id: String(chatId) },
      }),
    }
  );
  if (!res.ok) {
    console.log("repository_dispatch failed", res.status, await res.text());
  }
  return res.ok;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("InsideMove Agent webhook", { status: 200 });
    }

    // Telegram echoes the secret you registered with setWebhook. Without this
    // check the endpoint is a public trigger for your Actions minutes.
    const secret = request.headers.get("x-telegram-bot-api-secret-token");
    if (!env.TELEGRAM_SECRET || secret !== env.TELEGRAM_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }

    const callback = update.callback_query;
    const message = update.message;
    const chatId =
      callback?.message?.chat?.id ?? message?.chat?.id ?? null;

    // Always 200 to Telegram, even when ignoring — a non-2xx makes it retry
    // the same update for hours.
    if (env.TELEGRAM_CHAT_ID && String(chatId) !== String(env.TELEGRAM_CHAT_ID)) {
      console.log("ignoring update from chat", chatId);
      return new Response("ignored", { status: 200 });
    }

    if (callback) {
      // Answer first: this is what stops the button spinning. Deliberately
      // awaited rather than backgrounded — it is one fast call, and if it
      // fails the tap looks dead no matter what Actions does later.
      await telegram(env, "answerCallbackQuery", {
        callback_query_id: callback.id,
        text: ackFor(callback.data),
      });
      ctx.waitUntil(dispatch(env, update, chatId));
      return new Response("ok", { status: 200 });
    }

    if (message) {
      const text = (message.text || "").trim().toLowerCase();
      if (!text.startsWith("/")) {
        return new Response("ok", { status: 200 });
      }
      // /chatid is answered here so you can obtain the id before the rest of
      // the pipeline is configured.
      if (text.startsWith("/chatid")) {
        await telegram(env, "sendMessage", {
          chat_id: chatId,
          text: `This chat id is <code>${chatId}</code>`,
          parse_mode: "HTML",
        });
        return new Response("ok", { status: 200 });
      }
      if (text.startsWith("/scan")) {
        await telegram(env, "sendMessage", {
          chat_id: chatId,
          text: "🔍 Scan queued — results in a few minutes.",
        });
      }
      ctx.waitUntil(dispatch(env, update, chatId));
      return new Response("ok", { status: 200 });
    }

    return new Response("ok", { status: 200 });
  },
};
