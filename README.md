# 🧭 InsideMove Agent

A fully automated Telegram bot that scans the S&P 500 + NASDAQ after every US
close, surfaces the day's most significant price action, and walks you through
a three-stage research funnel via inline buttons.

```
Daily scan  →  ✅ Approve  →  Equity Research Note  →  📊 PTC  →  Technical Chart Analysis
    (cron)        (tap)          (FACT / INFERENCE)        (tap)      (chart + 3-6 day plan)
```

It runs entirely server-side on its own schedule. No Claude.ai session, no
manual trigger, nothing to keep open.

---

## What it does

### Stage 1 — the daily scan (15:00 MYT, weekdays)

The US session closes at 04:00 MYT. By 15:00 MYT the same day, the most recent
completed session's data is ~11 hours old — fresh enough to act on or add to a
watchlist the same day.

**The trigger only ever looks at the single most recently completed session.**
Longer history is used to *normalise* (20-day average volume, ATR, the
52-week range) but never to decide inclusion. A stock appears today because
today's tape did something, not because of a historical pattern match.

Two tiers qualify:

**Primary — threshold move.** The session's move crossed ±7%. Both directions,
since big drops are as tradeable as big pops. These always fill the day's slots
first, ranked by magnitude.

**Secondary — interesting price action.** The move was smaller than 7%, but the
tape did something structurally notable. This was your addition to the original
spec, and it's what keeps quiet days useful. Six signals are computed:

| Signal | Fires when |
|---|---|
| Volume spike | Volume ≥ 3× the 20-day average on a ≥3% move |
| Gap | Opened ≥4% away from the prior close and held the direction |
| 52-week breakout | Closed above the prior year's high, or below its low |
| Range expansion | Session true range ≥ 2.5× ATR(14) |
| Capitulation / exhaustion wick | Wick ≥60% of the session range on ≥2× volume |
| MA cross | SMA20 crossed SMA50 on this session |

A secondary name needs **at least two** confirming signals to earn a slot — one
volume spike on its own is far too common to be worth your attention. They take
at most 2 of the 5 daily slots, so a busy day is never crowded out by them.

Everything is filtered for liquidity first: minimum $3 price and $5M average
daily dollar volume.

### Stage 2 — the alert

Ticker, company, sector, the trailing-24h move (prior close vs most recent
close — the same single-session figure that triggered the scan, never a
multi-day number), volume ratio, a plain-language list of why it flagged, and a
one-line catalyst.

**The catalyst blurb is hard-filtered to headlines published inside the same
trailing 24-hour window as the price move.** If nothing falls in that window,
the bot says so — *"No confirmed catalyst found in the last 24h — move may be
technical or flow-driven"* — rather than reaching for a stale, unrelated story.

Buttons: `✅ Approve` `❌ Dismiss`

### Stage 3 — equity research note

On Approve, the bot assembles price/volume, same-window news, Form 4 insider
filings, analyst consensus and price targets, and peer tickers' same-day
performance, then renders a six-section note:

1. **Proximate Catalyst** — what happened and when
2. **Fundamentals vs. Move** — consensus PT, discount/premium in $ and %, justified or overreaction
3. **Insider Activity** — Form 4s, with routine comp (grants, 10b5-1 style) separated from discretionary open-market decisions
4. **Peer Context** — idiosyncratic or sector-wide
5. **What to Watch** — 2-3 concrete near-term checkpoints
6. **Verdict** — one paragraph

**Every claim is labelled FACT or INFERENCE.** FACT means the value is present
in the source data. INFERENCE means it is a reading derived from those values —
by fixed rule under `template`, by the model under `anthropic`. You can always
tell which is which.

Buttons: `📊 PTC (Professional Trading Chart)` `❌ Dismiss`

### Stage 4 — technical chart analysis

On PTC, the bot computes the technical picture **deterministically from price
data** — SMA20/50, ATR(14), RSI(14), swing-pivot support and resistance,
volume ratios — renders a 90-day candlestick chart with the levels marked, and
writes the note from exactly those numbers.

Under `template` nothing is paraphrased, so no figure can drift. Under
`anthropic` the model is explicitly forbidden from introducing a number that is
not in the data block: it interprets, it never guesses levels.

Output: chart image, then support, resistance (`Level | Context | Implication`),
price position vs both MAs in $ and %, volume read, price-action structure,
a **3-6 day conditional plan** (if holds / if breaks / if bounces), ATR-based
sizing arithmetic, and a bottom line with its own strongest counterargument.

---

## Quick start

```bash
git clone <your-repo> insidemove-agent && cd insidemove-agent
cp .env.example .env       # only TELEGRAM_BOT_TOKEN is required
docker compose up -d --build
```

Then message your bot `/start` on Telegram.

### Verify the install first

```bash
python3 selftest.py
```

Runs offline — no network, no API keys. It checks RSI and ATR against Wilder's
published reference values, exercises every signal path, renders a chart, and
validates the Telegram HTML of all three note types. All 32 checks should pass
before you trust anything it tells you.

---

## Getting the keys

**1. Telegram bot token.** Message [@BotFather](https://t.me/BotFather), send
`/newbot`, name it *InsideMove Agent*, pick a username ending in `bot`. He
replies with a token like `8123456789:AAH...`. Paste into `TELEGRAM_BOT_TOKEN`.

**2. Telegram chat id.** Leave `TELEGRAM_CHAT_ID` blank, start the bot
(`docker compose up`), message it `/chatid`, and it replies with the number.
Put that in `.env` and restart. This also locks the bot to your chat — nobody
else can trigger scans against your API budget.

**3. An AI key — only if you want one.** The default note engine is
`template`: deterministic rendering, no API, no key, no cost. Set
`NOTE_ENGINE=anthropic` and add an [Anthropic key](https://console.anthropic.com)
only if you want model-written notes.

**4. Finnhub (optional, free).** [finnhub.io/register](https://finnhub.io/register).
Free tier, 60 calls/min. Without it, news falls back to Yahoo Finance and the
notes will report insider filings and analyst ratings as unavailable rather
than inventing them. Worth the two minutes.

---

## Deploying free on GitHub Actions (Path A)

No server, no monthly bill. The scan is a scheduled workflow; button taps
arrive through a Cloudflare Worker that answers Telegram instantly and then
starts an Actions job.

**1. Push this repo to GitHub.** Public gets unlimited Actions minutes;
private gets 2,000/month and the scan uses roughly 60 now that the price
cache exists. Either works.

**2. Add repository secrets** (Settings → Secrets and variables → Actions):
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and optionally `FINNHUB_API_KEY`.

**3. Run the scan once by hand** — Actions → Daily scan → Run workflow. This
builds the price cache and, importantly, registers the cron. Scheduled
workflows on new repos often do not fire for the first 12–48 hours until
they have been triggered once.

**4. Deploy the Worker:**

```bash
cd worker
npx wrangler deploy
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_CHAT_ID
npx wrangler secret put TELEGRAM_SECRET      # any random string
npx wrangler secret put GITHUB_TOKEN         # fine-grained PAT, Contents: read/write
npx wrangler secret put GITHUB_REPO          # you/insidemove-agent
```

**5. Point Telegram at it:**

```bash
python3 scripts/set_webhook.py --url https://insidemove-webhook.<you>.workers.dev
```

`TELEGRAM_SECRET` must match on both sides — Telegram echoes it on every
request and the Worker rejects anything without it. Without that check the
endpoint is a public trigger for your Actions minutes.

Webhook and long-polling are mutually exclusive. Run
`scripts/set_webhook.py --delete` before going back to `bot.py`.

### Two Actions quirks that will bite you otherwise

**Scheduled workflows are disabled after 60 days of repository inactivity.**
The `keepalive.yml` workflow commits a heartbeat every Sunday to prevent it.
Without it, the likeliest failure of this whole system is that it quietly
stops in two months and you notice when you realise you have not had an alert
in a while.

**Cron is queued, not precise.** A 07:00 UTC schedule typically starts
between 07:00 and 07:20. Since the US session closed hours earlier, this
does not matter here.

---

## Hosting — the paid alternatives

You said you had no idea on this, so here's the honest comparison.

**Recommended: a €4/month VPS.** [Hetzner](https://www.hetzner.com/cloud) CX22
(2 vCPU, 4GB) is the best value; [DigitalOcean](https://www.digitalocean.com)'s
$6 droplet is the same thing with nicer docs. Either handles this comfortably.
You get a persistent disk for the SQLite state, real cron, and nothing sleeps.
Setup below is ten minutes.

**Free alternative: Oracle Cloud Always Free.** Their ARM instance (4 cores,
24GB) is genuinely free forever and vastly overpowered for this. The catch is
the signup is fiddly and capacity in popular regions is often unavailable. If
you're willing to fight it once, it costs nothing.

**What I'd avoid.** Render and Railway free tiers sleep idle services, which
kills the long-poll listener. Lambda-style functions don't suit a process that
needs to hold a socket open all day — you'd need a second always-on component
anyway, so you may as well have one box.

### VPS setup, start to finish

```bash
# on a fresh Ubuntu 24.04 box, as root
apt update && apt install -y docker.io docker-compose-v2 git
timedatectl set-timezone Asia/Kuala_Lumpur

git clone <your-repo> /opt/insidemove-agent
cd /opt/insidemove-agent
cp .env.example .env
nano .env                      # paste your keys

python3 selftest.py            # optional but do it
docker compose up -d --build
docker compose logs -f bot     # should print "connected as @yourbotname"
```

That's it. The `cron` container fires the scan at 15:00 MYT on weekdays; the
`bot` container answers your button taps.

### Without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# the listener, under systemd
cp deploy/insidemove-bot.service /etc/systemd/system/
systemctl enable --now insidemove-bot

# the daily scan, under cron
crontab -e
# 0 15 * * 1-5 cd /opt/insidemove-agent && .venv/bin/python run_scan.py >> data/scan.log 2>&1
```

---

## Running costs

| Item | Cost |
|---|---|
| Price data (Yahoo via `yfinance`) | free, no key |
| News, insiders, analyst ratings (Finnhub free tier) | free |
| Note rendering (`NOTE_ENGINE=template`, the default) | free, no key |
| Note rendering (`NOTE_ENGINE=anthropic`, optional) | ~$0.11/trading day |
| VPS | €3.79-6/month |

**On the default settings the only cost is hosting.** Template mode renders
every note from data that was already computed, so there is no API bill at all.

If you do turn on `anthropic`, expect ~$6/month at five alerts a day with two
approvals. A middle option: `NOTE_ENGINE=template` with
`STAGE3_ENGINE=anthropic` pays for the equity note alone — the one place a
model genuinely adds synthesis — for roughly $1/month.

> Note on data providers: the original spec called for Financial Modeling Prep's
> bulk EOD endpoint. FMP now gates bulk delivery, insider trades and analyst
> data behind their Ultimate tier (~$1,788/year), which is wildly out of
> proportion for a five-alerts-a-day bot. Yahoo + Finnhub's free tiers cover the
> same ground at zero cost. `data.py` is the only file that touches price data,
> so if you ever want to swap in a paid provider, that's the one file to rewrite.

---

## Configuration

Every threshold is an environment variable — see `.env.example`. The ones worth
knowing:

| Variable | Default | What it does |
|---|---|---|
| `MOVE_THRESHOLD_PCT` | `7.0` | Primary trigger, absolute % |
| `DIRECTION` | `both` | `both`, `up`, or `down` |
| `MAX_ALERTS_PER_DAY` | `5` | Daily cap |
| `MAX_SECONDARY_SLOTS` | `2` | How many slots price-action names may take |
| `UNIVERSE_MODE` | `wide` | `wide` = +all NASDAQ-listed; `core` = S&P 500 + NDX only |
| `NEWS_WINDOW_HOURS` | `24` | Catalyst freshness window |
| `MIN_AVG_DOLLAR_VOLUME` | `5000000` | Liquidity floor |
| `NOTE_ENGINE` | `template` | `template` (free) or `anthropic` |
| `STAGE3_ENGINE` | inherits | Override the engine for the equity note only |

`UNIVERSE_MODE=wide` is the default deliberately: 7% single-session moves are
rare in the S&P 500, and the names that actually move are usually mid caps. If
you only want large caps, set it to `core`.

## Commands

| Command | Effect |
|---|---|
| `/scan` | Run a scan now, off-schedule |
| `/status` | Config, universe size, last scan |
| `/chatid` | Print this chat's id |
| `/help` | The funnel explained |

## Layout

```
run_scan.py            cron entrypoint  (Stage 1 + 2)
bot.py                 long-poll listener (Stage 3 + 4)
selftest.py            offline verification
insidemove/
  config.py            every threshold, env-overridable
  universe.py          S&P 500 + NASDAQ constituents, weekly cache
  data.py              batched OHLCV — the only file that touches a price provider
  signals.py           Stage 1 trigger + secondary price-action engine
  indicators.py        SMA / ATR / RSI / pivots / support & resistance
  news.py              hard 24h publish-timestamp filter
  notes.py             engine dispatcher — template vs anthropic
  templates.py         deterministic note rendering, no LLM
  fundamentals.py      insiders, analyst view, peers
  charting.py          candlestick + volume + levels → PNG
  ai.py                optional Claude engine (only if NOTE_ENGINE=anthropic)
  telegram_api.py      Bot API client
  scanner.py           Stage 1 + 2 orchestration
  handlers.py          Stage 3 + 4 button handling
  state.py             small JSON state — alerts, universe, offset
  cache.py             incremental price cache with split detection
  callbacks.py         self-contained button payloads (no database)
```

## Design notes

**Why two processes.** The scan is a cron job; the listener is always-on. A
crash in one never takes the other down, and the scan doesn't hold a socket
open for 23 hours a day.

**Why there is no database.** Telegram caps `callback_data` at 64 bytes, and
`a:AAPL:20260904` is fifteen — the ticker and session fit in the button
itself. That removed the token table, and with it the need for any persistent
disk, which is what lets the whole bot run on GitHub Actions. What little
state remains (which alerts went out, the ticker universe) is a couple of
small JSON files.

**Why the price cache verifies before it appends.** Yahoo serves unadjusted
prices, so the morning after a 4-for-1 split every historical bar it returns
is a quarter of what was cached yesterday. Appending blindly would leave
pre-split prices sitting next to post-split ones and quietly corrupt every
percentage and level computed from them. Each refresh re-fetches an
overlapping window and compares closes on dates it already holds; a ticker
whose history has been restated is dropped and re-downloaded in full.

**Why template mode is the default.** Almost everything the Stage 4 note says
was already computed deterministically before any model was called — the
levels, the MA distances, the volume ratio, the ATR sizing arithmetic, the
conditional plan. A model was only phrasing it. Rendering it directly costs
nothing and guarantees no number drifts between what was computed and what you
read. Stage 3 is the honest exception: weighing a catalyst against targets,
insiders and peers is real synthesis, which is why `STAGE3_ENGINE` exists.

**Why the indicators are hand-rolled.** They're validated against Wilder's
published reference values in `selftest.py`, and they use TradingView's RMA
seeding convention — so the RSI and ATR the bot quotes match what you see when
you pull the chart up yourself. A plain `ewm()` does not, which is a subtle way
to lose trust in every number the bot prints.

**Why charts are rendered locally.** QuickChart has no native candlestick type
and would need 90 bars stuffed into a URL. Drawing with matplotlib is free, has
no request-size ceiling, and guarantees the chart and the write-up come from the
same data.

---

## Disclaimer

This produces research and analysis, not investment advice. Every AI-generated
section separates what is sourced from what is interpreted precisely so you can
apply your own judgment to the second category. Verify anything you act on.
