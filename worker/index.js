/**
 * InsideMove Agent — Telegram webhook receiver *and* signup backend.
 *
 * Five jobs, all inside Cloudflare's free tier:
 *
 *   GET  /            the signup page (served from here, so the page and the
 *                     API share an origin and no CORS is involved)
 *   GET  /slots       {used, total} for the live slot counter
 *   POST /claim       {chat_id, code} -> verifies, stores, welcomes
 *   GET  /allowlist   the bot reads this at scan time (shared-secret header)
 *   POST /            the Telegram webhook (the original job)
 *
 * Why the Worker holds the allowlist rather than a GitHub secret: a claim has
 * to take effect without a human editing anything. KV is readable in ~1ms
 * from the webhook path, so a newly claimed chat can tap buttons immediately,
 * and the daily scan picks the list up when it runs.
 *
 * The claim code is an HMAC of the chat id under SIGNUP_SECRET, truncated to
 * six characters. That is what stops a stranger claiming slots for chat ids
 * they do not own: the code can only come from the bot's own /start reply.
 *
 * Secrets (wrangler secret put <NAME>):
 *   TELEGRAM_BOT_TOKEN     from @BotFather
 *   TELEGRAM_SECRET        any random string; also given to setWebhook
 *   TELEGRAM_CHAT_ID       the operator's chat id — exempt from every limit
 *   GITHUB_TOKEN           fine-grained PAT, Contents: read/write on this repo
 *   GITHUB_REPO            "owner/insidemove-agent"
 *   SIGNUP_SECRET          any random string; must match the bot's SIGNUP_SECRET
 *
 * Bindings (wrangler.toml):
 *   CLAIMS                 KV namespace holding claim:<chat_id> records
 *
 * Vars (wrangler.toml):
 *   TOTAL_SLOTS            how many guests may claim; default 20
 */

const ACK = {
  approve: "Researching…",
  ptc: "Building chart…",
  dismiss: "Dismissed",
};

const CODE_LENGTH = 6;
const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

function ackFor(data) {
  const verb = String(data || "").split(":")[0];
  return { a: ACK.approve, p: ACK.ptc, d: ACK.dismiss }[verb] || "Working…";
}

/** RFC 4648 base32, so the Worker and Python derive identical codes. */
function base32(bytes) {
  let bits = 0;
  let value = 0;
  let out = "";
  for (const byte of bytes) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      out += B32[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += B32[(value << (5 - bits)) & 31];
  return out;
}

async function claimCode(env, chatId) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(env.SIGNUP_SECRET || ""),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const mac = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(String(chatId))
  );
  return base32(new Uint8Array(mac)).slice(0, CODE_LENGTH);
}

/** Constant-time-ish compare, so a wrong code leaks no timing information. */
function sameCode(a, b) {
  const x = String(a || "").toUpperCase();
  const y = String(b || "").toUpperCase();
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

async function telegram(env, method, body) {
  return fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function totalSlots(env) {
  const n = parseInt(env.TOTAL_SLOTS || "20", 10);
  return Number.isFinite(n) && n > 0 ? n : 20;
}

async function listClaims(env) {
  const out = [];
  let cursor;
  // KV list pages at 1000 keys; a 20-slot bot never gets near that, but the
  // loop costs nothing and means raising TOTAL_SLOTS cannot break counting.
  do {
    const page = await env.CLAIMS.list({ prefix: "claim:", cursor });
    for (const key of page.keys) out.push(key.name.slice("claim:".length));
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
  return out;
}

async function isClaimed(env, chatId) {
  if (!env.CLAIMS) return false;
  return (await env.CLAIMS.get(`claim:${chatId}`)) !== null;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
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

// ---------------------------------------------------------------------------
// signup API
// ---------------------------------------------------------------------------

async function handleSlots(env) {
  if (!env.CLAIMS) return json({ used: 0, total: totalSlots(env), ready: false });
  const claims = await listClaims(env);
  return json({ used: claims.length, total: totalSlots(env), ready: true });
}

async function handleClaim(request, env) {
  if (!env.CLAIMS || !env.SIGNUP_SECRET) {
    return json({ ok: false, error: "Signup is not configured yet." }, 503);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ ok: false, error: "Bad request." }, 400);
  }

  const chatId = String(body.chat_id || "").trim();
  const code = String(body.code || "").trim();

  if (!/^-?\d{5,20}$/.test(chatId)) {
    return json({
      ok: false,
      error: "That does not look like a chat ID. It is a number, 5-20 digits.",
    }, 400);
  }

  const expected = await claimCode(env, chatId);
  if (!sameCode(code, expected)) {
    return json({
      ok: false,
      error:
        "That code does not match that chat ID. Send /start to the bot again " +
        "and copy both lines exactly as it gives them.",
    }, 403);
  }

  const already = await env.CLAIMS.get(`claim:${chatId}`);
  const claims = await listClaims(env);
  const total = totalSlots(env);

  if (already) {
    return json({
      ok: true, already: true, used: claims.length, total,
      message: "You already have a slot — alerts arrive with the next daily scan.",
    });
  }

  if (claims.length >= total) {
    return json({
      ok: false, used: claims.length, total,
      error: `All ${total} slots are taken. Nothing to do but wait for someone to lose interest.`,
    }, 409);
  }

  // Verifying by *messaging* them: Telegram refuses to let a bot write to
  // someone who never started it, so a success here proves the chat is real
  // and reachable, and the same call delivers the welcome.
  const welcome = await telegram(env, "sendMessage", {
    chat_id: chatId,
    parse_mode: "HTML",
    text:
      "✅ <b>Slot claimed — welcome to InsideMove Agent</b>\n\n" +
      "Every weekday after the US close I scan the S&amp;P 500 and NASDAQ and " +
      "send you the day's biggest moves: the size of the move, why it flagged, " +
      "and the catalyst that explains it, linked to its source.\n\n" +
      "<b>Buttons</b>\n" +
      "✅ Approve → equity research note\n" +
      "📊 PTC → chart plus technical analysis\n" +
      "❌ Dismiss → clears the buttons, free\n\n" +
      "<b>Fair use</b>\n" +
      "Research taps are capped per person per day — they run real work " +
      "against shared API limits. Send /quota to see what you have left. " +
      "Scan alerts themselves are unlimited.\n\n" +
      "<i>Information only — not financial advice. Verify every catalyst at " +
      "its source before acting.</i>",
  });

  const payload = await welcome.json().catch(() => ({}));
  if (!payload.ok) {
    return json({
      ok: false, used: claims.length, total,
      error:
        "Telegram would not let me message that chat. Open the bot and send " +
        "/start first — Telegram blocks bots from messaging people who have " +
        "not started them.",
    }, 400);
  }

  await env.CLAIMS.put(
    `claim:${chatId}`,
    JSON.stringify({ at: new Date().toISOString(), via: "web" })
  );

  return json({
    ok: true, used: claims.length + 1, total,
    message: "Slot claimed. Check Telegram — the welcome message is already there.",
  });
}

async function handleAllowlist(request, env) {
  // The bot reads this. A shared secret rather than a public endpoint, since
  // the list is a set of real people's chat ids.
  const given = request.headers.get("x-insidemove-key");
  if (!env.SIGNUP_SECRET || given !== env.SIGNUP_SECRET) {
    return json({ error: "forbidden" }, 403);
  }
  if (!env.CLAIMS) return json({ chat_ids: [], total: totalSlots(env) });
  const claims = await listClaims(env);
  return json({ chat_ids: claims, used: claims.length, total: totalSlots(env) });
}

// ---------------------------------------------------------------------------
// Telegram webhook
// ---------------------------------------------------------------------------

async function handleWebhook(request, env, ctx) {
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
  const chatId = callback?.message?.chat?.id ?? message?.chat?.id ?? null;
  const isOwner =
    env.TELEGRAM_CHAT_ID && String(chatId) === String(env.TELEGRAM_CHAT_ID);
  const allowed = isOwner || (await isClaimed(env, chatId));

  // Always 200, even when ignoring: a non-2xx makes Telegram retry the same
  // update for hours.
  if (!allowed) {
    // A stranger. Answered entirely here — it costs nothing, where a
    // repository_dispatch would spend CI minutes on someone with no slot.
    if (message) {
      const text = (message.text || "").trim().toLowerCase();
      if (text.startsWith("/start") || text.startsWith("/help") || text.startsWith("/chatid")) {
        const code = await claimCode(env, chatId);
        const used = env.CLAIMS ? (await listClaims(env)).length : 0;
        const site = env.SIGNUP_URL || "the signup page";
        await telegram(env, "sendMessage", {
          chat_id: chatId,
          parse_mode: "HTML",
          text:
            "🔒 <b>InsideMove Agent is private.</b>\n\n" +
            `It runs on one person's API credit, so seats are limited — ` +
            `<b>${used} of ${totalSlots(env)}</b> taken right now.\n\n` +
            "To claim one, go to the signup page and enter these two lines:\n\n" +
            `Chat ID: <code>${chatId}</code>\n` +
            `Claim code: <code>${code}</code>\n\n` +
            `${site}\n\n` +
            "<i>Both lines are yours alone. The code will not work for any " +
            "other chat ID.</i>",
        });
      }
    } else if (callback) {
      await telegram(env, "answerCallbackQuery", {
        callback_query_id: callback.id,
        text: "This bot is private — claim a slot on the signup page first.",
        show_alert: true,
      });
    }
    return new Response("ignored", { status: 200 });
  }

  if (callback) {
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
    if (text.startsWith("/chatid")) {
      await telegram(env, "sendMessage", {
        chat_id: chatId,
        text: `This chat id is <code>${chatId}</code>`,
        parse_mode: "HTML",
      });
      return new Response("ok", { status: 200 });
    }
    if (text.startsWith("/scan") && !isOwner) {
      await telegram(env, "sendMessage", {
        chat_id: chatId,
        text: "Only the operator can start a scan. The daily one reaches you automatically.",
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
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "GET") {
      if (path === "/") {
        return new Response(PAGE(totalSlots(env), env.BOT_USERNAME || "your_bot"), {
          headers: { "content-type": "text/html; charset=utf-8" },
        });
      }
      if (path === "/slots") return handleSlots(env);
      if (path === "/allowlist") return handleAllowlist(request, env);
      return new Response("not found", { status: 404 });
    }

    if (request.method === "POST") {
      if (path === "/claim") return handleClaim(request, env);
      return handleWebhook(request, env, ctx);
    }

    return new Response("method not allowed", { status: 405 });
  },
};

// ---------------------------------------------------------------------------
// the signup page
// ---------------------------------------------------------------------------

const PAGE = (total, botUser) => `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>InsideMove Agent — claim a slot</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#fff; --ink:#15181d; --muted:#5b6472; --line:#e3e7ed;
    --accent:#1f6feb; --accent-ink:#fff; --ok:#15803d; --ok-bg:#eaf7ee;
    --warn:#9a3412; --warn-bg:#fdf1e7; --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  @media (prefers-color-scheme:dark){
    :root:not([data-theme="light"]){
      --bg:#0e1116; --card:#161b22; --ink:#e7ecf3; --muted:#9aa5b4; --line:#262d37;
      --accent:#4c8dff; --accent-ink:#06121f; --ok:#6ee7a0; --ok-bg:#10281b;
      --warn:#fdba74; --warn-bg:#2a1c10;
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased}
  .wrap{max-width:720px;margin:0 auto;padding:32px 20px 72px}
  h1{font-size:clamp(28px,6vw,40px);line-height:1.15;letter-spacing:-.02em;margin:0 0 8px}
  h2{font-size:19px;letter-spacing:-.01em;margin:0 0 14px}
  p{margin:0 0 14px}
  .lede{color:var(--muted);font-size:17px;margin-bottom:28px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
    padding:22px;margin-bottom:18px}
  code,kbd{font-family:var(--mono);font-size:.92em;background:var(--bg);
    border:1px solid var(--line);border-radius:5px;padding:1px 5px}
  /* slot meter */
  .meter{display:flex;align-items:baseline;gap:10px;margin:0 0 12px}
  .meter b{font-size:40px;letter-spacing:-.03em;line-height:1;font-variant-numeric:tabular-nums}
  .meter span{color:var(--muted)}
  .bar{height:10px;border-radius:99px;background:var(--line);overflow:hidden}
  .bar i{display:block;height:100%;width:0;border-radius:99px;background:var(--accent);
    transition:width .5s cubic-bezier(.22,1,.36,1)}
  .pips{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}
  .pip{width:22px;height:22px;border-radius:6px;background:var(--line)}
  .pip.on{background:var(--accent)}
  /* steps */
  ol.steps{counter-reset:s;list-style:none;margin:0;padding:0}
  ol.steps li{counter-increment:s;position:relative;padding:0 0 18px 44px}
  ol.steps li::before{content:counter(s);position:absolute;left:0;top:-1px;
    width:28px;height:28px;border-radius:99px;background:var(--accent);
    color:var(--accent-ink);font-weight:650;font-size:14px;
    display:grid;place-items:center}
  ol.steps li:last-child{padding-bottom:0}
  .quote{border-left:3px solid var(--line);padding:2px 0 2px 14px;color:var(--muted);
    font-size:15px;margin:10px 0}
  /* form */
  label{display:block;font-weight:600;font-size:14px;margin:14px 0 6px}
  input{width:100%;padding:13px 14px;font:16px var(--mono);color:var(--ink);
    background:var(--bg);border:1px solid var(--line);border-radius:10px}
  input:focus{outline:2px solid var(--accent);outline-offset:1px}
  button{width:100%;margin-top:18px;padding:14px 18px;font:650 16px system-ui;
    color:var(--accent-ink);background:var(--accent);border:0;border-radius:10px;
    cursor:pointer}
  button:disabled{opacity:.55;cursor:not-allowed}
  .msg{margin-top:14px;padding:13px 15px;border-radius:10px;font-size:15px;display:none}
  .msg.ok{display:block;background:var(--ok-bg);color:var(--ok)}
  .msg.bad{display:block;background:var(--warn-bg);color:var(--warn)}
  .hint{color:var(--muted);font-size:13.5px;margin-top:8px}
  details{border-top:1px solid var(--line);padding:14px 0 0;margin-top:6px}
  summary{cursor:pointer;font-weight:600}
  .fine{color:var(--muted);font-size:13.5px;line-height:1.65}
  ul.plain{margin:0 0 14px;padding-left:20px}
  ul.plain li{margin-bottom:7px}
  footer{color:var(--muted);font-size:13px;text-align:center;margin-top:26px}
</style>
</head><body>
<div class="wrap">

  <h1>InsideMove Agent</h1>
  <p class="lede">A Telegram bot that scans the S&amp;P 500 and NASDAQ after every
  US close and tells you which stocks moved — and, more usefully, <em>why</em>.
  Free to you. Limited seats, because it runs on one guy's API credit.</p>

  <div class="card">
    <h2>Seats</h2>
    <div class="meter">
      <b id="used">—</b><span>of ${total} taken</span>
    </div>
    <div class="bar"><i id="fill"></i></div>
    <div class="pips" id="pips"></div>
    <p class="hint" id="slotnote">Checking…</p>
  </div>

  <div class="card">
    <h2>How to claim one</h2>
    <ol class="steps">
      <li><strong>Find the bot.</strong> Open Telegram, search
        <code>@${botUser}</code>, and open the chat.</li>
      <li><strong>Send <code>/start</code>.</strong> It will tell you the bot is
        private — that is expected, not a rejection. The same message hands you
        two lines:
        <div class="quote">Chat ID: <code>123456789</code><br>
        Claim code: <code>K7F2QA</code></div>
      </li>
      <li><strong>Paste both below and claim.</strong> You will get a welcome
        message in Telegram within seconds. Alerts start on the next weekday
        scan, about 3pm Malaysia time.</li>
    </ol>
  </div>

  <div class="card">
    <h2>Claim your seat</h2>
    <form id="form" autocomplete="off">
      <label for="chat">Chat ID</label>
      <input id="chat" inputmode="numeric" placeholder="123456789" required>
      <label for="code">Claim code</label>
      <input id="code" maxlength="6" placeholder="K7F2QA" required
             style="text-transform:uppercase">
      <button id="go" type="submit">Claim my seat</button>
    </form>
    <div class="msg" id="msg"></div>
    <p class="hint">The code only works for the chat ID it was issued to, so
    nobody can claim a seat on your behalf — or burn one with a made-up number.</p>
  </div>

  <div class="card">
    <h2>Things worth knowing</h2>
    <ul class="plain">
      <li><strong>You need no API key, no account, no Claude subscription.</strong>
      Everything runs on the operator's keys. You just need Telegram.</li>
      <li><strong>Scan alerts are unlimited.</strong> Every weekday, up to five
      names, with the catalyst behind each one linked to its source.</li>
      <li><strong>Research buttons are rationed.</strong> ✅ Approve and 📊 PTC
      each run real work against shared API limits, so you get a few per day.
      Send <code>/quota</code> to the bot to see what is left. ❌ Dismiss is free
      and always will be.</li>
      <li><strong>Why the cap exists.</strong> Every tap spends a few minutes of
      compute and a handful of API calls on somebody's personal account. Without
      a cap, twenty enthusiastic people on a volatile Tuesday could turn one
      hobby project into a bill that needs explaining to a spouse. The cap is
      cheaper than that conversation.</li>
      <li><strong>If a seat is full, it is full.</strong> There is no waitlist,
      no premium tier and no "contact sales". There is one operator and he has
      a day job.</li>
    </ul>
    <details>
      <summary>Disclaimer — the part that matters</summary>
      <p class="fine" style="margin-top:12px">
      <strong>This is information, not financial advice.</strong> InsideMove
      Agent reports price moves and links to news and regulatory filings that
      may explain them. It does not know your finances, your goals, your time
      horizon or your risk tolerance, and it is not a licensed adviser,
      broker or analyst. Nothing it sends is a recommendation to buy, sell or
      hold anything.</p>
      <p class="fine"><strong>Verify before you act.</strong> Every catalyst
      carries a link to its source — a filing, a press release, a news story.
      Data comes from third-party feeds that can be delayed, incomplete or
      wrong, and the bot's labels are produced by pattern-matching rules, not
      judgement. A headline the bot calls a catalyst may be coincidence.</p>
      <p class="fine"><strong>Large moves cut both ways.</strong> The stocks
      here qualified by moving violently, which is precisely the population
      where losses arrive fastest. Past moves predict nothing.</p>
      <p class="fine"><strong>No warranty, no liability.</strong> The service
      is provided as-is, may break, may go quiet for days, and may be
      withdrawn at any time without notice. The operator accepts no liability
      for any loss arising from its use or its absence. Your trades are
      yours alone.</p>
      <p class="fine">Your chat ID is stored so the bot knows where to send
      alerts, and for nothing else. Ask and it is deleted.</p>
    </details>
  </div>

  <footer>Not affiliated with Telegram, NASDAQ, or anyone's broker.<br>
  Built for one person, shared with ${total}.</footer>
</div>

<script>
  const TOTAL = ${total};
  const $ = (id) => document.getElementById(id);

  function paint(used, total) {
    $("used").textContent = used;
    const pct = total ? Math.min(100, (used / total) * 100) : 0;
    $("fill").style.width = pct + "%";
    const pips = $("pips");
    pips.innerHTML = "";
    for (let i = 0; i < total; i++) {
      const d = document.createElement("div");
      d.className = "pip" + (i < used ? " on" : "");
      pips.appendChild(d);
    }
    const left = Math.max(0, total - used);
    $("slotnote").textContent = left === 0
      ? "All seats taken. Nothing to claim right now."
      : left === 1 ? "One seat left." : left + " seats left.";
    $("go").disabled = left === 0;
  }

  async function refresh() {
    try {
      const r = await fetch("/slots", { cache: "no-store" });
      const d = await r.json();
      paint(d.used ?? 0, d.total ?? TOTAL);
    } catch {
      $("slotnote").textContent = "Could not check seat count — the form still works.";
    }
  }

  $("form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("msg");
    const btn = $("go");
    msg.className = "msg";
    btn.disabled = true;
    btn.textContent = "Claiming…";
    try {
      const r = await fetch("/claim", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          chat_id: $("chat").value.trim(),
          code: $("code").value.trim().toUpperCase(),
        }),
      });
      const d = await r.json();
      if (typeof d.used === "number") paint(d.used, d.total ?? TOTAL);
      if (d.ok) {
        msg.className = "msg ok";
        msg.textContent = "✅ " + (d.message || "Seat claimed.");
        $("form").reset();
      } else {
        msg.className = "msg bad";
        msg.textContent = "⚠️ " + (d.error || "Could not claim that seat.");
      }
    } catch {
      msg.className = "msg bad";
      msg.textContent = "⚠️ Network error — try again in a moment.";
    } finally {
      btn.textContent = "Claim my seat";
      refresh();
    }
  });

  refresh();
  setInterval(refresh, 30000);
</script>
</body></html>`;
