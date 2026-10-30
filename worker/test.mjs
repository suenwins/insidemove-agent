/**
 * Worker tests — run before every deploy:   node worker/test.mjs
 *
 * No Cloudflare account, no network: KV is a Map and Telegram/GitHub are
 * intercepted, so the signup flow, the seat accounting and the webhook's
 * stranger handling are all exercised locally in about a second.
 */
const mod = await import("./index.js");
const worker = mod.default;
let telegramCalls = [], dispatches = [], tgOk = true;
globalThis.fetch = async (url, opts) => {
  const u = String(url);
  if (u.includes('api.telegram.org')) {
    telegramCalls.push({ method: u.split('/').pop(), body: JSON.parse(opts.body) });
    return new Response(JSON.stringify({ ok: tgOk, description: tgOk ? '' : 'bot was blocked' }));
  }
  if (u.includes('api.github.com')) { dispatches.push(JSON.parse(opts.body)); return new Response('{}', {status:200}); }
  return new Response('{}');
};
function kv() {
  const m = new Map();
  return {
    get: async k => (m.has(k) ? m.get(k) : null),
    put: async (k, v) => { m.set(k, v); },
    delete: async k => { m.delete(k); },
    list: async ({prefix, cursor}) => ({ keys:[...m.keys()].filter(k=>k.startsWith(prefix)).map(name=>({name})), list_complete:true }),
    _m: m,
  };
}
const env = { CLAIMS: kv(), SIGNUP_SECRET:'test-secret-123', TELEGRAM_BOT_TOKEN:'t', TELEGRAM_SECRET:'wh', TELEGRAM_CHAT_ID:'111', GITHUB_REPO:'x/y', GITHUB_TOKEN:'g', TOTAL_SLOTS:'2', BOT_USERNAME:'b', SIGNUP_URL:'https://s' };
const ctx = { waitUntil: p => p };
const get = (p, h={}) => worker.fetch(new Request('https://w.dev'+p, {headers:h}), env, ctx);
const post = (p, body, h={}) => worker.fetch(new Request('https://w.dev'+p, {method:'POST', headers:{'content-type':'application/json',...h}, body:JSON.stringify(body)}), env, ctx);
const B32="ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const b32=(bytes)=>{let bits=0,v=0,o="";for(const b of bytes){v=(v<<8)|b;bits+=8;while(bits>=5){o+=B32[(v>>>(bits-5))&31];bits-=5;}}if(bits>0)o+=B32[(v<<(5-bits))&31];return o;};
const codeFor=async(id)=>{const k=await crypto.subtle.importKey("raw",new TextEncoder().encode('test-secret-123'),{name:"HMAC",hash:"SHA-256"},false,["sign"]);
  return b32(new Uint8Array(await crypto.subtle.sign("HMAC",k,new TextEncoder().encode(String(id))))).slice(0,6);};
let pass=0, fail=0;
const ok = (label, cond, detail='') => { if (cond) {pass++; console.log('  ✓', label);} else {fail++; console.log('  ✗', label, detail);} };

let r = await get('/'); let html = await r.text();
ok('GET / serves the page', r.status===200 && html.includes('Claim my seat'));
ok('page shows the configured bot username', html.includes('@b'));
ok('page shows the seat total', html.includes('of 2 taken'));

r = await get('/slots'); let d = await r.json();
ok('slots starts empty', d.used===0 && d.total===2, JSON.stringify(d));

r = await post('/claim', {chat_id:'123456789', code:'WRONG1'}); d = await r.json();
ok('a wrong code is refused', r.status===403 && !d.ok);
r = await post('/claim', {chat_id:'abc', code:'TD5Q5W'}); d = await r.json();
ok('a non-numeric chat id is refused', r.status===400);
r = await post('/claim', {chat_id:'123456789', code:'td5q5w'}); d = await r.json();
ok('the right code works, case-insensitively', r.status===200 && d.ok===true && d.used===1, JSON.stringify(d));
ok('claiming sends the welcome message', telegramCalls.some(c=>c.method==='sendMessage' && c.body.text.includes('Slot claimed')));
r = await post('/claim', {chat_id:'123456789', code:'TD5Q5W'}); d = await r.json();
ok('claiming twice is idempotent', d.ok===true && d.already===true && d.used===1, JSON.stringify(d));

tgOk = false;
r = await post('/claim', {chat_id:'555000222', code:await codeFor('555000222')}); d = await r.json();
ok('a chat that never started the bot is refused', !d.ok && d.error.includes('/start'), JSON.stringify(d));
ok('and no seat is consumed', (await (await get('/slots')).json()).used===1);
tgOk = true;
r = await post('/claim', {chat_id:'555000222', code:await codeFor('555000222')}); d = await r.json();
ok('seat 2 of 2 claims fine', d.ok===true && d.used===2, JSON.stringify(d));
r = await post('/claim', {chat_id:'987654321', code:await codeFor('987654321')}); d = await r.json();
ok('a third claim is refused once the seats are gone', !d.ok && r.status===409 && d.error.includes('All 2 slots'), JSON.stringify(d));
r = await post('/claim', {chat_id:'111', code:'BADBAD'}); d = await r.json();
ok('a short chat id (under 5 digits) is treated as malformed', r.status===400 || r.status===403);

r = await get('/allowlist'); ok('/allowlist without the key is forbidden', r.status===403);
r = await get('/allowlist', {'x-insidemove-key':'test-secret-123'}); d = await r.json();
ok('/allowlist returns the claimed ids', d.chat_ids.length===2 && d.chat_ids.includes('123456789') && d.chat_ids.includes('555000222'), JSON.stringify(d));

telegramCalls=[]; dispatches=[];
const wh = (body) => worker.fetch(new Request('https://w.dev/', {method:'POST', headers:{'content-type':'application/json','x-telegram-bot-api-secret-token':'wh'}, body:JSON.stringify(body)}), env, ctx);
r = await wh({message:{chat:{id:999999999}, text:'/start'}});
ok('a stranger gets the private reply', telegramCalls.some(c=>c.body.text?.includes('is private')));
ok('the reply carries their chat id and code', telegramCalls.some(c=>c.body.text?.includes('999999999')));
ok('no CI run is spent on a stranger', dispatches.length===0);
telegramCalls=[]; dispatches=[];
r = await wh({message:{chat:{id:123456789}, text:'/status'}});
ok('a claimed user reaches the bot', dispatches.length===1);
telegramCalls=[]; dispatches=[];
r = await wh({callback_query:{id:'c1', data:'a:AAPL:20261023:p', message:{chat:{id:123456789}, message_id:5}}});
ok('a claimed user\'s button tap is answered then dispatched',
   telegramCalls.some(c=>c.method==='answerCallbackQuery') && dispatches.length===1);
telegramCalls=[]; dispatches=[];
r = await wh({callback_query:{id:'c2', data:'a:AAPL:20261023:p', message:{chat:{id:888}, message_id:5}}});
ok('a stranger\'s tap is refused without a CI run',
   dispatches.length===0 && telegramCalls.some(c=>c.body.text?.includes('private')));
r = await worker.fetch(new Request('https://w.dev/', {method:'POST', body:'{}'}), env, ctx);
ok('the webhook rejects a request without Telegram\'s secret', r.status===403);
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
