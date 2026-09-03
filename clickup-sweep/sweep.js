#!/usr/bin/env node
'use strict';
/*
 * ClickUp card-sweep → fleet PM dispatcher.
 *
 * Drives the blog-production state machine that lives on ClickUp cards. Polls the Marketing list and,
 * for each card in an ACTIONABLE status, dispatches an async PM turn on OpenClaw (/hooks/agent) telling
 * it which card + what to do. It does NOT wait on the PM (the PM turn is slow and may spawn specialists),
 * so PM latency never causes a sweep timeout — the PM advances the card status itself, which is what makes
 * the sweep stop re-dispatching. Ported from the retired marketing-agent sweep.sh, pointed at the new PM.
 *
 * Actionable statuses -> action:
 *   to do            -> run the blog-production pipeline from the brief on the card
 *   approved         -> publish the approved draft (blog-production Phase 2) + arm socials
 *   rejected/changes -> rework per the client's feedback on the card, then back to in review
 *
 * Idempotency: a state file records (taskId -> lastDispatchedStatus + ts). We skip a card whose status
 * we already dispatched, unless it has been >REDISPATCH_MIN minutes (in case the PM stalled). The PM
 * moving the card off the actionable status is the primary guard.
 *
 * Skips cards assigned ONLY to humans with no bot involvement? No — the fleet owns these cards; we act on
 * status. We DO skip cards already in a terminal/in-flight status (in progress, in review, completed, Closed).
 */
const fs = require('fs');

const WS            = process.env.CLICKUP_WORKSPACE || '307311';
const LIST_ID       = process.env.MARKETING_LIST || '901613842998';
const PK            = process.env.CLICKUP_PK_TOKEN || '';
const API           = 'https://api.clickup.com';
const OPENCLAW_URL  = (process.env.OPENCLAW_URL || 'http://openclaw-s13f8pdutxps4w5z3fbl9lq5:8080').replace(/\/$/, '');
const HOOK_TOKEN    = process.env.OPENCLAW_HOOK_TOKEN || '';       // x-openclaw-token for /hooks/agent (SECRET)
const PM_AGENT_ID   = process.env.PM_AGENT_ID || 'pm';
const POLL_SECONDS  = Number(process.env.POLL_SECONDS || '90');
const REDISPATCH_MIN= Number(process.env.REDISPATCH_MIN || '20');
const STATE_FILE    = process.env.STATE_FILE || '/data/.sweep-state.json';
const HOOK_TIMEOUT_MS = Number(process.env.HOOK_TIMEOUT_MS || '30000');
// SAFETY GATE: publishing an "approved" card pushes to the LIVE site + arms live socials (irreversible).
// Default OFF so the sweep never auto-publishes to production; flip to "true" once a human has confirmed
// the staged draft. When off, approved cards are logged and skipped, not dispatched.
const PUBLISH_ON_APPROVED = String(process.env.PUBLISH_ON_APPROVED || 'false').toLowerCase() === 'true';

// status (lower-cased) -> directive for the PM
const ACTIONS = {
  'to do':    'is NEW work at status "to do". Load the blog-production skill and run it from the brief on this card (SEO → writer → image → QA → stage the WordPress draft + social drafts → move it to "in review" and send the client the review links). Do NOT publish; stop at in review.',
  'approved': 'has been APPROVED by the client. Load the blog-production skill decision path: publish the approved draft to WordPress (publisher Phase 2), verify it is live, arm the social/GMB posts, and move this card to "completed". The draft + metadata are on this card / in the project note.',
  'rejected': 'was REJECTED / has change requests. Load the blog-production skill: read the client\'s requested changes from the card comments, loop the specific feedback to the responsible specialist, re-QA, update the existing WordPress draft + social drafts, and move it back to "in review".',
  'changes':  'has client change requests. Load the blog-production skill: apply the requested changes from the card comments (rework via the responsible specialist, re-QA, update the existing draft), then move it back to "in review".',
};
// statuses we never act on (in-flight / terminal)
const SKIP = new Set(['in progress', 'in review', 'waiting on client', 'completed', 'closed', 'blocked']);

if (!PK) throw new Error('CLICKUP_PK_TOKEN is required');
if (!HOOK_TOKEN) throw new Error('OPENCLAW_HOOK_TOKEN is required');

const log = (...a) => console.log(new Date().toISOString(), ...a);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function cu(path, opts = {}, tries = 3) {
  let lastErr;
  for (let i = 0; i < tries; i++) {
    try {
      const res = await fetch(API + path, { headers: { Authorization: PK, 'Content-Type': 'application/json' }, ...opts });
      const txt = await res.text();
      if (res.status >= 500) throw new Error(`${res.status} transient`);
      if (!res.ok) throw new Error(`${opts.method || 'GET'} ${path} -> ${res.status} ${txt.slice(0, 200)}`);
      return txt ? JSON.parse(txt) : {};
    } catch (e) { lastErr = e; if (i < tries - 1) { await sleep(1500 * (i + 1)); continue; } }
  }
  throw lastErr;
}

function loadState() { try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch (e) { return {}; } }
function saveState(s) { fs.mkdirSync(require('path').dirname(STATE_FILE), { recursive: true }); fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2)); }

// Fire-and-forget async PM dispatch via OpenClaw /hooks/agent. Returns {ok, runId} or {ok:false}.
async function dispatchPM(task, directive) {
  const msg = [
    `ClickUp card ${task.id} ("${task.name}") in the Marketing list ${directive}`,
    ``,
    `Card: ${task.url}`,
    `Act deterministically per the blog-production skill; the status machine lives on this card. Do only`,
    `what you can verify — never claim work you did not do. Update the card status as you progress.`,
  ].join('\n');
  const body = {
    message: msg,
    agentId: PM_AGENT_ID,
    name: `CardSweep:${task.id}`,
    idempotencyKey: `sweep-${task.id}-${(task.status || '').toLowerCase().replace(/\s+/g, '_')}`,
    deliver: false,
  };
  const ctl = AbortSignal.timeout ? AbortSignal.timeout(HOOK_TIMEOUT_MS) : undefined;
  const res = await fetch(`${OPENCLAW_URL}/hooks/agent`, {
    method: 'POST',
    headers: { 'x-openclaw-token': HOOK_TOKEN, 'Content-Type': 'application/json' },
    body: JSON.stringify(body), signal: ctl,
  });
  const txt = await res.text();
  if (!res.ok) throw new Error(`hook ${res.status} ${txt.slice(0, 200)}`);
  const p = JSON.parse(txt || '{}');
  return { ok: !!p.ok, runId: p.runId };
}

async function sweepOnce() {
  const d = await cu(`/api/v2/list/${LIST_ID}/task?subtasks=true&include_closed=false`);
  const tasks = d.tasks || [];
  const state = loadState();
  const now = Date.now();
  let acted = 0;
  for (const t of tasks) {
    const status = (t.status && t.status.status || '').toLowerCase();
    if (SKIP.has(status)) continue;
    const directive = ACTIONS[status];
    if (!directive) continue;                     // not an actionable status
    if (status === 'approved' && !PUBLISH_ON_APPROVED) {
      log(`GATED approved card ${t.id} "${(t.name || '').slice(0, 60)}" — PUBLISH_ON_APPROVED=false; NOT dispatching a live publish`);
      continue;
    }
    const prev = state[t.id];
    // already dispatched this exact status recently? skip (avoid duplicate PM runs)
    if (prev && prev.status === status && (now - prev.ts) < REDISPATCH_MIN * 60000) continue;
    try {
      const r = await dispatchPM({ id: t.id, name: t.name, url: t.url, status }, directive);
      state[t.id] = { status, ts: now, runId: r.runId || null };
      saveState(state);
      acted++;
      log(`dispatched PM for ${t.id} [${status}] "${(t.name || '').slice(0, 60)}" -> runId=${r.runId || '?'}`);
    } catch (e) {
      log(`dispatch FAILED for ${t.id} [${status}] - ${(e.message || '').slice(0, 140)}`);
    }
  }
  if (acted === 0) log(`swept ${tasks.length} card(s); nothing actionable`);
}

(async function loop() {
  log(`clickup-sweep starting: ws=${WS} list=${LIST_ID} pm=${PM_AGENT_ID} poll=${POLL_SECONDS}s publishOnApproved=${PUBLISH_ON_APPROVED}`);
  for (;;) {
    try { await sweepOnce(); }
    catch (e) { log('sweep error:', (e.message || '').slice(0, 200)); }
    await sleep(POLL_SECONDS * 1000);
  }
})();
