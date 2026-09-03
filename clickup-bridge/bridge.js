#!/usr/bin/env node
'use strict';
/*
 * ClickUp <-> fleet PM chat bridge.
 *
 * Makes ClickUp chat the PM's conversational channel: polls the workspace's DM + GROUP_DM channels,
 * relays each NEW human message to the fleet PM (OpenClaw agent `openclaw/pm`, which has ClickUp +
 * all fleet tools), and posts the PM's reply back into the same ClickUp channel. Ported from the
 * retired marketing-agent bridge (clickup-chat-bridge.js) — the proven ClickUp polling, mention-
 * gating and per-channel state are kept; the one seam that used to shell out to `claude -p` now runs
 * a real PM turn on OpenClaw (same path mcp-a2a's ask_pm uses).
 *
 * ClickUp is the PM's channel; Webster/Hermes (the external-client front-door) is NOT in this loop.
 * The bridge posts replies under the PM/AI-Agent ClickUp identity (WEBSTER_UID), and skips that
 * user's own messages, so there is no reply loop.
 *
 * Runs as a container on the fleet network. Config via env (below). Internal only.
 */
const fs = require('fs');

// --- config (env) ---------------------------------------------------------------------------------
const WS          = process.env.CLICKUP_WORKSPACE || '307311';           // Web Intelligenz workspace
const WEBSTER_UID = process.env.WEBSTER_UID || '106813628';              // AI-Agent ClickUp user (skip own msgs; @mention target)
const PK          = process.env.CLICKUP_PK_TOKEN || '';                  // ClickUp personal token (SECRET)
const API         = 'https://api.clickup.com';
const OPENCLAW_URL= (process.env.OPENCLAW_URL || 'http://openclaw-s13f8pdutxps4w5z3fbl9lq5:8080').replace(/\/$/, '');
const OC_USER     = process.env.OPENCLAW_BASIC_USER || '';               // OpenClaw nginx basic-auth user (SECRET)
const OC_PASS     = process.env.OPENCLAW_BASIC_PASS || '';               // OpenClaw nginx basic-auth pass (SECRET)
const PM_MODEL    = process.env.PM_MODEL || 'openclaw/pm';
const PM_TIMEOUT_MS = Number(process.env.PM_TIMEOUT_MS || '240000');
const POLL_SECONDS= Number(process.env.POLL_SECONDS || '20');
const STATE_FILE  = process.env.STATE_FILE || '/data/.chat-state.json';
const MSG_LIMIT   = 30;
const HISTORY_CTX = 6;
// ClickUp renders a mention as `[@Webster](#user_mention#<uid>)`; the plain-text `@webster` counts too.
const MENTION_RE  = new RegExp(`#user_mention#${WEBSTER_UID}\\b|@webster\\b`, 'i');

if (!PK) throw new Error('CLICKUP_PK_TOKEN is required');
if (!OC_USER || !OC_PASS) throw new Error('OPENCLAW_BASIC_USER/OPENCLAW_BASIC_PASS are required');

const log = (...a) => console.log(new Date().toISOString(), ...a);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// --- ClickUp REST (retry transient 5xx) -----------------------------------------------------------
async function cu(path, opts = {}, tries = 3) {
  let lastErr;
  for (let i = 0; i < tries; i++) {
    try {
      const res = await fetch(API + path, { headers: { Authorization: PK, 'Content-Type': 'application/json' }, ...opts });
      const txt = await res.text();
      if (res.status >= 500 && res.status < 600) throw new Error(`${res.status} transient`);
      if (!res.ok) throw new Error(`${opts.method || 'GET'} ${path} -> ${res.status} ${txt.slice(0, 200)}`);
      return txt ? JSON.parse(txt) : {};
    } catch (e) { lastErr = e; if (i < tries - 1) { await sleep(1500 * (i + 1)); continue; } }
  }
  throw lastErr;
}

// --- PM turn on OpenClaw (replaces the old `claude -p` seam; same path as mcp-a2a ask_pm) ----------
async function askPM(systemCtx, userText) {
  const headers = { 'Content-Type': 'application/json',
                    Authorization: 'Basic ' + Buffer.from(`${OC_USER}:${OC_PASS}`).toString('base64') };
  const body = { model: PM_MODEL, messages: [ { role: 'system', content: systemCtx }, { role: 'user', content: userText } ] };
  const ctl = AbortSignal.timeout ? AbortSignal.timeout(PM_TIMEOUT_MS) : undefined;
  const res = await fetch(`${OPENCLAW_URL}/v1/chat/completions`, { method: 'POST', headers, body: JSON.stringify(body), signal: ctl });
  const txt = await res.text();
  if (!res.ok) throw new Error(`pm ${res.status} ${txt.slice(0, 200)}`);
  const p = JSON.parse(txt || '{}');
  const reply = p.choices && p.choices[0] && p.choices[0].message && p.choices[0].message.content;
  return (reply || '').trim();
}

function loadState() { try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch (e) { return null; } }
function saveState(s) { fs.mkdirSync(require('path').dirname(STATE_FILE), { recursive: true }); fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2)); }

function extractAttachments(content) {
  const urls = []; const re = /!\[[^\]]*\]\((https?:\/\/[^)]+)\)/g;
  let m; while ((m = re.exec(content)) !== null) urls.push(m[1]);
  return urls;
}
// ClickUp escapes markdown punctuation in message content; undo it before the PM reads it.
function unescapeMd(s) { return String(s || '').replace(/\\([\[\]\\`*_{}()#+.!-])/g, '$1'); }

// System framing for the PM turn. The PM owns ClickUp + its skills; this just sets the situation and
// the reply contract (the bridge posts the returned text; the PM must NOT post chat itself).
function systemCtx(channelId, isGroup) {
  return [
    `You are the fleet PM. This is a message from the Web Intelligenz team in ClickUp chat channel ${channelId}`
      + (isGroup ? ` (a SHARED channel — several people are in it and you were @mentioned).` : ` (a direct message).`),
    `Handle it exactly as you normally would: triage it, create or update the ClickUp card, and start the`,
    `appropriate pipeline if it is real work (delegate to specialists — do not do their jobs yourself).`,
    `Record this ClickUp channel id (${channelId}) on the card so progress can be reported back here later.`,
    `Do NOT send a ClickUp chat message yourself — your text reply is posted back into this channel`,
    `automatically. Keep the reply concise (2-4 sentences), naming the card you created or updated.`,
  ].join('\n');
}

function buildUserText({ userText, uid, history, attachments, isGroup }) {
  const historyBlock = history.length ? history.map(h => `  ${h.who}: ${h.text.slice(0, 200)}`).join('\n') : '  (none)';
  const attachBlock = attachments.length
    ? `\nAttachments the sender included (URLs only — you cannot see the image contents):\n` + attachments.map(u => '  ' + u).join('\n')
    : '';
  return [
    isGroup
      ? `New message(s) in the shared channel (ClickUp user ${uid} tagged you; the actual request may be from someone else), newest last:`
      : `New message from ClickUp user ${uid}:`,
    `"""`, userText, `"""` + attachBlock,
    ``,
    `Recent conversation for context (most recent last):`, historyBlock,
  ].join('\n');
}

async function deliver(channelId, isGroup, meta) {
  let reply;
  try { reply = await askPM(systemCtx(channelId, isGroup), buildUserText(meta)); }
  catch (e) { log('askPM FAILED for', channelId, '-', (e.message || '').slice(0, 200)); reply = null; }
  try {
    const text = reply || "Sorry — I hit a problem handling that just now. Could you resend it? (Logged for the team.)";
    await cu(`/api/v3/workspaces/${WS}/chat/channels/${channelId}/messages`,
      { method: 'POST', body: JSON.stringify({ type: 'message', content: text }) });
    log('replied in', channelId, '(' + text.length + ' chars)');
  } catch (e) { log('reply POST failed for', channelId, '-', (e.message || '').slice(0, 120)); }
}

async function pollOnce() {
  const chans = (await cu(`/api/v3/workspaces/${WS}/chat/channels?limit=100`)).data || [];
  const rooms = chans.filter(c => c.type === 'DM' || c.type === 'GROUP_DM');
  let state = loadState();
  const firstEver = state === null;
  if (firstEver) state = {};

  for (const dm of rooms) {
    const isGroup = dm.type !== 'DM';
    const msgs = ((await cu(`/api/v3/workspaces/${WS}/chat/channels/${dm.id}/messages?limit=${MSG_LIMIT}`)).data || [])
      .slice().sort((a, b) => Number(a.date) - Number(b.date));
    const newestDate = msgs.length ? Number(msgs[msgs.length - 1].date) : 0;
    if (firstEver) { state[dm.id] = newestDate; continue; }   // baseline; no replies on first run
    let lastSeen = state[dm.id]; if (lastSeen === undefined) lastSeen = 0;
    const fresh = msgs.filter(m => Number(m.date) > Number(lastSeen) && String(m.user_id) !== WEBSTER_UID);
    if (state[dm.id] === undefined) state[dm.id] = newestDate;
    if (!fresh.length) continue;

    const historyAll = msgs.map(m => ({ who: String(m.user_id) === WEBSTER_UID ? 'PM' : ('user ' + m.user_id), text: String(m.content || '') }));

    // Shared channels: stay silent unless @mentioned, but mark the batch read so it isn't re-evaluated.
    if (isGroup && !fresh.some(m => MENTION_RE.test(String(m.content || '')))) {
      state[dm.id] = Math.max(Number(state[dm.id] || 0), newestDate); saveState(state);
      log('group', dm.id, '-', fresh.length, 'new msg(s), not addressed; skipped');
      continue;
    }

    if (isGroup) {
      const anchor = fresh.slice().reverse().find(m => MENTION_RE.test(String(m.content || ''))) || fresh[fresh.length - 1];
      const userText = fresh.map(m => `[user ${m.user_id}]: ${unescapeMd(m.content)}`).join('\n');
      const attachments = fresh.flatMap(m => extractAttachments(String(m.content || '')));
      const idx = historyAll.findIndex(h => h.text === String(fresh[0].content || ''));
      const history = idx > 0 ? historyAll.slice(Math.max(0, idx - HISTORY_CTX), idx) : [];
      log('GROUP', dm.id, 'tagged by', anchor.user_id, '->', fresh.length, 'msg(s)', attachments.length ? `(+${attachments.length} attach)` : '');
      await deliver(dm.id, true, { userText, uid: anchor.user_id, history, attachments, isGroup: true });
      state[dm.id] = Math.max(Number(state[dm.id] || 0), newestDate); saveState(state);
      continue;
    }

    for (const m of fresh) {
      const userText = unescapeMd(m.content);
      const attachments = extractAttachments(String(m.content || ''));
      log('DM', dm.id, 'from', m.user_id, '->', userText.slice(0, 80), attachments.length ? `(+${attachments.length} attach)` : '');
      const idx = historyAll.findIndex(h => h.text === String(m.content || ''));
      const history = idx > 0 ? historyAll.slice(Math.max(0, idx - HISTORY_CTX), idx) : [];
      await deliver(dm.id, false, { userText, uid: m.user_id, history, attachments, isGroup: false });
      state[dm.id] = Math.max(Number(state[dm.id] || 0), Number(m.date)); saveState(state);   // per-message persist
    }
  }
  saveState(state);
  if (firstEver) log('first run: baselined', Object.keys(state).length, 'channel(s); no replies sent');
}

(async function loop() {
  log('clickup-pm-bridge starting: ws=' + WS + ' pm=' + PM_MODEL + ' poll=' + POLL_SECONDS + 's');
  for (;;) {
    try { await pollOnce(); }
    catch (e) { log('poll error:', (e.message || '').slice(0, 200)); }
    await sleep(POLL_SECONDS * 1000);
  }
})();
