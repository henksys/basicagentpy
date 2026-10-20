// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Henk Duisterwinkel
// basicagentpy web — vanilla JavaScript, no dependencies.
//
// Talks to the server's JSON API with the bearer token from the URL fragment
// or sessionStorage, renders the chat NDJSON stream with fetch() streaming
// (so the Authorization header works), and keeps every dynamic value out of
// innerHTML: assistant and tool text is always set with textContent.

(function () {
  'use strict';

  // ===== Tiny DOM helpers ====================================================

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function setStatus(node, text, kind) {
    if (!text) {
      node.hidden = true;
      node.textContent = '';
      return;
    }
    node.hidden = false;
    node.textContent = text;
    node.className = `status-line ${kind || ''}`.trim();
  }

  let toastTimer = null;

  function hideToast() {
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = null;
    $('#toast').hidden = true;
  }

  function showToast(text, kind = 'ok') {
    const toast = $('#toast');
    toast.textContent = text;
    toast.className = `toast ${kind || ''}`.trim();
    toast.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toastTimer = null;
      toast.hidden = true;
    }, 4500);
  }

  $('#toast').addEventListener('click', hideToast);

  function formatTime(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
  }

  // ===== Token ================================================================

  const TOKEN_KEY = 'basicagentpy-token';
  let token = sessionStorage.getItem(TOKEN_KEY) || '';

  (function readTokenFromHash() {
    const match = location.hash.match(/token=([^&]+)/);
    if (!match) return;
    token = decodeURIComponent(match[1]);
    sessionStorage.setItem(TOKEN_KEY, token);
    history.replaceState(null, '', location.pathname + location.search);
  })();

  function showTokenGate(message) {
    $('#token-gate').hidden = false;
    const errorNode = $('#token-error');
    if (message) {
      errorNode.textContent = message;
      errorNode.hidden = false;
    }
    $('#token-input').focus();
  }

  $('#token-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const value = $('#token-input').value.trim();
    if (!value) return;
    sessionStorage.setItem(TOKEN_KEY, value);
    location.reload();
  });

  // ===== API ==================================================================

  async function api(path, options = {}) {
    const { method = 'GET', body } = options;
    const headers = { authorization: `Bearer ${token}` };
    if (body !== undefined) headers['content-type'] = 'application/json';
    const response = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined
    });
    let data = null;
    try {
      data = await response.json();
    } catch {
      // Some error responses have no JSON body.
    }
    if (response.status === 401) {
      showTokenGate('The token was rejected. Paste the current token from the server output.');
      throw new Error('unauthorized');
    }
    if (!response.ok) throw new Error(data?.error || `Request failed (HTTP ${response.status})`);
    return data;
  }

  // ===== Theme ================================================================

  $('#theme-toggle').addEventListener('click', () => {
    const root = document.documentElement;
    const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try {
      localStorage.setItem('henksys-theme', next);
    } catch {
      // Private mode: the toggle still works for this page view.
    }
  });

  // ===== Views ================================================================

  const VIEWS = ['chat', 'settings', 'memory'];

  function showView(name) {
    if (!VIEWS.includes(name)) name = 'chat';
    for (const view of VIEWS) $(`#view-${view}`).hidden = view !== name;
    for (const button of $$('#tabs .tab-link')) {
      if (button.dataset.view === name) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    }
    if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
    if (name === 'settings') loadSettings();
    if (name === 'memory') loadMemoryView();
  }

  for (const button of $$('#tabs .tab-link')) {
    button.addEventListener('click', () => showView(button.dataset.view));
  }
  window.addEventListener('hashchange', () => showView(location.hash.slice(1)));

  // ===== Status ===============================================================

  let status = null;

  async function loadStatus() {
    status = await api('/api/status');
    $('#model-chip').textContent = status.model;
    $('#footer-version').textContent = status.version ? `v${status.version}` : '';
    $('#data-dir').textContent = status.dataDir || 'memory disabled';
    $('#chat-sub').textContent = `${status.model} · ${status.cwd}`;
    renderContextGauge();
    return status;
  }

  // ===== Chat =================================================================

  const chat = { sessionId: null, busy: false, assistantEl: null, pendingTools: [], contextSize: null };

  // Compact character count for the context gauge (999, 1.5k, 25k).
  function formatChars(value) {
    const size = Number(value) || 0;
    if (size < 1000) return String(size);
    if (size < 10000) return `${(size / 1000).toFixed(1)}k`;
    return `${Math.round(size / 1000)}k`;
  }

  // The gauge compares the serialized conversation size (the same metric the
  // server uses to trim history) with the agent's auto-trim budget; at 100%
  // the oldest messages are dropped before the next request.
  function renderContextGauge() {
    const gauge = $('#context-gauge');
    const limit = status?.contextChars;
    if (!limit || chat.contextSize == null) {
      gauge.hidden = true;
      return;
    }
    const percent = Math.min(100, Math.round((chat.contextSize / limit) * 100));
    gauge.hidden = false;
    gauge.textContent = `context ${percent}% (${formatChars(chat.contextSize)}/${formatChars(limit)} chars)`;
    gauge.className = `context-gauge${percent >= 90 ? ' full' : ''}`;
    gauge.title = "Serialized conversation size against the agent's auto-trim budget; at 100% the oldest messages are dropped.";
  }

  function scrollMessages() {
    const messages = $('#messages');
    messages.scrollTop = messages.scrollHeight;
  }

  function addMessage(role, text) {
    const message = el('article', `msg msg-${role}`);
    message.append(el('div', 'msg-role', role === 'user' ? 'you' : 'basicagentpy'));
    const body = el('div', 'msg-text');
    body.textContent = text;
    message.append(body);
    $('#messages').append(message);
    scrollMessages();
    return body;
  }

  function addNotice(text, kind = '') {
    const notice = el('div', `notice ${kind}`.trim(), text);
    $('#messages').append(notice);
    scrollMessages();
  }

  function addToolCard(name, args) {
    const card = el('div', 'tool');
    const head = el('div', 'tool-head');
    head.append(el('span', 'tool-name', name));
    const statusChip = el('span', 'chip', 'running');
    head.append(statusChip);
    const details = document.createElement('details');
    details.append(el('summary', '', 'arguments'));
    details.append(el('pre', '', JSON.stringify(args ?? {}, null, 2)));
    card.append(head, details);
    $('#messages').append(card);
    scrollMessages();
    const entry = { card, statusChip };
    chat.pendingTools.push(entry);
    return entry;
  }

  function finishToolCard(event) {
    const entry = chat.pendingTools.shift();
    if (!entry) return;
    entry.statusChip.textContent = event.ok ? `ok · ${event.durationMs} ms` : `failed · ${event.durationMs} ms`;
    entry.statusChip.className = `chip ${event.ok ? 'chip-ok' : 'chip-error'}`;
    const resultText = event.result === undefined
      ? ''
      : typeof event.result === 'string' ? event.result : JSON.stringify(event.result, null, 2);
    if (resultText) {
      const details = document.createElement('details');
      details.append(el('summary', '', 'result'));
      details.append(el('pre', '', resultText));
      entry.card.append(details);
    }
    scrollMessages();
  }

  function addApprovalCard(event) {
    const card = el('div', 'approval');
    card.dataset.approvalId = event.id;
    card.append(el('p', 'approval-text', `Approval required: ${event.description}`));
    const actions = el('div', 'approval-actions');
    const stateLabel = el('span', 'approval-state', 'waiting for your answer');
    const approveButton = el('button', 'btn btn-small btn-primary', 'Approve');
    const denyButton = el('button', 'btn btn-small btn-ghost', 'Deny');
    approveButton.type = 'button';
    denyButton.type = 'button';
    const answer = async (approved) => {
      approveButton.disabled = true;
      denyButton.disabled = true;
      stateLabel.textContent = approved ? 'approved' : 'denied';
      try {
        await api(`/api/approvals/${encodeURIComponent(event.id)}`, { method: 'POST', body: { approved } });
      } catch (error) {
        stateLabel.textContent = error.message;
      }
    };
    approveButton.addEventListener('click', () => answer(true));
    denyButton.addEventListener('click', () => answer(false));
    actions.append(approveButton, denyButton, stateLabel);
    card.append(actions);
    $('#messages').append(card);
    scrollMessages();
  }

  function resolveApprovalCard(id, approved) {
    const card = $(`[data-approval-id="${CSS.escape(id)}"]`);
    if (!card) return;
    for (const button of $$('button', card)) button.disabled = true;
    const state = $('.approval-state', card);
    if (state) state.textContent = approved ? 'approved' : 'denied';
  }

  function setComposerHint(text) {
    $('#composer-hint').textContent = text;
  }

  function handleEvent(event) {
    switch (event.type) {
      case 'iteration':
        setComposerHint(`working… (iteration ${event.iteration})`);
        if (typeof event.contextSize === 'number') {
          chat.contextSize = event.contextSize;
          renderContextGauge();
        }
        break;
      case 'assistant_delta': {
        // Keep the answer chronological: start a fresh bubble whenever the
        // last one is no longer the newest element (tool/approval cards were
        // appended after it), so the final answer stays below the approvals.
        const messages = $('#messages');
        if (!chat.assistantEl || messages.lastElementChild !== chat.assistantEl.parentElement) {
          chat.assistantEl = addMessage('assistant', '');
        }
        chat.assistantEl.textContent += event.text;
        scrollMessages();
        break;
      }
      case 'tool_start':
        addToolCard(event.name, event.args);
        break;
      case 'tool_end':
        finishToolCard(event);
        break;
      case 'approval':
        addApprovalCard(event);
        break;
      case 'approval_result':
        resolveApprovalCard(event.id, event.approved);
        break;
      case 'warning':
        addNotice(event.message, 'warn');
        break;
      case 'error':
        addNotice(event.message, 'error');
        break;
      case 'aborted':
        addNotice('Turn aborted.', 'warn');
        break;
      default:
        break;
    }
  }

  async function readNDJSON(body) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index;
      while ((index = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (line) {
          try {
            handleEvent(JSON.parse(line));
          } catch {
            // Ignore malformed stream lines.
          }
        }
      }
    }
    if (buffer.trim()) {
      try {
        handleEvent(JSON.parse(buffer.trim()));
      } catch {
        // Ignore malformed stream lines.
      }
    }
  }

  function updateBusyUI() {
    $('#stop').hidden = !chat.busy;
    $('#send').disabled = chat.busy;
    $('#prompt').disabled = chat.busy;
    if (chat.busy) $('#prompt').blur();
    else $('#prompt').focus();
  }

  async function sendMessage(message) {
    if (chat.busy || !message.trim()) return;
    if (!chat.sessionId) await newSession();
    chat.busy = true;
    updateBusyUI();
    addMessage('user', message);
    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
        body: JSON.stringify({ sessionId: chat.sessionId, message })
      });
      if (response.status === 401) {
        showTokenGate('The token was rejected. Paste the current token from the server output.');
        throw new Error('unauthorized');
      }
      if (!response.ok) {
        let detail = null;
        try {
          detail = await response.json();
        } catch {
          // No JSON body.
        }
        throw new Error(detail?.error || `Request failed (HTTP ${response.status})`);
      }
      await readNDJSON(response.body);
    } catch (error) {
      addNotice(`error: ${error.message}`, 'error');
    } finally {
      chat.busy = false;
      chat.assistantEl = null;
      chat.pendingTools = [];
      updateBusyUI();
      setComposerHint('');
      loadSessionList().catch(() => {});
    }
  }

  async function newSession() {
    const data = await api('/api/sessions', { method: 'POST', body: {} });
    activateSession(data.id, data.messages, data.contextSize ?? 0);
  }

  async function openSession(id) {
    const data = await api(`/api/sessions/${encodeURIComponent(id)}`);
    activateSession(data.id, data.messages, data.contextSize ?? 0);
  }

  function activateSession(id, messages, contextSize = 0) {
    chat.sessionId = id;
    $('#messages').textContent = '';
    chat.assistantEl = null;
    chat.pendingTools = [];
    chat.contextSize = contextSize;
    renderContextGauge();
    $('#chat-title').textContent = id;
    if (messages && messages.length) {
      for (const message of messages) addMessage(message.role, message.content);
    } else {
      addNotice('New conversation. Ask basicagentpy anything — it can read and write files and use skills.');
    }
    loadSessionList().catch(() => {});
  }

  async function loadSessionList() {
    const data = await api('/api/sessions');
    const list = $('#session-list');
    list.textContent = '';
    $('#sessions-hint').textContent = data.memoryEnabled ? '' : 'Memory is disabled; conversations are not saved.';
    for (const session of data.sessions) {
      const item = document.createElement('li');
      const button = el('button', 'session-open');
      button.type = 'button';
      button.append(el('span', 'session-title', session.title || '(empty)'));
      button.append(el('span', 'session-meta', `${session.id} · ${session.messages} msgs`));
      if (session.id === chat.sessionId) button.setAttribute('aria-current', 'true');
      button.addEventListener('click', () => {
        openSession(session.id).catch((error) => addNotice(`error: ${error.message}`, 'error'));
        button.blur();
      });
      item.append(button);
      list.append(item);
    }
  }

  $('#composer').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = $('#prompt');
    const text = input.value.trim();
    if (!text || chat.busy) return;
    input.value = '';
    sendMessage(text);
  });

  $('#prompt').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      $('#composer').requestSubmit();
    }
  });

  $('#stop').addEventListener('click', () => {
    api('/api/chat/abort', { method: 'POST', body: { sessionId: chat.sessionId } }).catch(() => {});
  });

  $('#new-session').addEventListener('click', () => {
    newSession().catch((error) => addNotice(`error: ${error.message}`, 'error'));
  });

  const SESSIONS_KEY = 'basicagentpy-sessions';

  function applySessionsState(state) {
    $('.chat-layout').dataset.sessions = state;
    $('#sessions-toggle').setAttribute('aria-expanded', state === 'open' ? 'true' : 'false');
  }

  $('#sessions-toggle').addEventListener('click', () => {
    const next = $('.chat-layout').dataset.sessions === 'closed' ? 'open' : 'closed';
    applySessionsState(next);
    try {
      localStorage.setItem(SESSIONS_KEY, next);
    } catch {
      // Private mode: the toggle still works for this page view.
    }
  });

  (function initSessionsState() {
    let state = 'open';
    try {
      if (localStorage.getItem(SESSIONS_KEY) === 'closed') state = 'closed';
    } catch {
      // Ignore and stay open.
    }
    applySessionsState(state);
  })();

  // ===== Settings =============================================================

  let configPayload = null;

  function getByPath(object, path) {
    return path.split('.').reduce((value, key) => (value == null ? undefined : value[key]), object);
  }

  function setByPath(object, path, value) {
    const keys = path.split('.');
    let current = object;
    for (const key of keys.slice(0, -1)) {
      if (current[key] == null || typeof current[key] !== 'object' || Array.isArray(current[key])) current[key] = {};
      current = current[key];
    }
    current[keys[keys.length - 1]] = value;
  }

  function deleteByPath(object, path) {
    const keys = path.split('.');
    const parents = [object];
    let current = object;
    for (const key of keys.slice(0, -1)) {
      if (current[key] == null || typeof current[key] !== 'object') return;
      current = current[key];
      parents.push(current);
    }
    delete current[keys[keys.length - 1]];
    for (let index = parents.length - 1; index > 0; index--) {
      if (Object.keys(parents[index]).length === 0) delete parents[index - 1][keys[index - 1]];
      else break;
    }
  }

  function addRootRow(path = '', access = '') {
    const row = el('div', 'root-row');
    const pathInput = el('input');
    pathInput.type = 'text';
    pathInput.placeholder = '.';
    pathInput.value = path;
    const accessSelect = document.createElement('select');
    for (const level of ['rw', 'ro', 'approval', 'deny']) {
      const option = document.createElement('option');
      option.value = level;
      option.textContent = level;
      accessSelect.append(option);
    }
    accessSelect.value = access || 'rw';
    const remove = el('button', 'btn btn-small btn-ghost remove-root', 'Remove');
    remove.type = 'button';
    remove.addEventListener('click', () => row.remove());
    row.append(pathInput, accessSelect, remove);
    $('#roots-list').append(row);
  }

  function renderRoots(roots) {
    $('#roots-list').textContent = '';
    const rows = Array.isArray(roots) && roots.length ? roots : [{ path: '', access: '' }];
    for (const root of rows) addRootRow(root.path ?? '', root.access ?? '');
  }

  function fillSettings(payload) {
    configPayload = payload;
    const raw = payload.raw || {};
    const effective = payload.effective || {};
    $('#config-path').textContent = payload.path;
    $('#api-key-hint').textContent = payload.apiKeySet ? '(a key is set; leave empty to keep it)' : '(no key stored in the file)';
    for (const input of $$('[data-config]')) {
      const path = input.dataset.config;
      if (input.type === 'checkbox') {
        const rawValue = getByPath(raw, path);
        input.checked = rawValue === undefined ? Boolean(getByPath(effective, path)) : Boolean(rawValue);
        continue;
      }
      const value = getByPath(raw, path);
      input.value = value === undefined || value === null ? '' : String(value);
    }
    for (const textarea of $$('[data-config-list]')) {
      const value = getByPath(raw, textarea.dataset.configList);
      textarea.value = Array.isArray(value) ? value.join('\n') : '';
    }
    renderRoots(raw.tools?.files?.roots);
    $('#raw-config').value = JSON.stringify(raw, null, 2);
  }

  function collectSettings() {
    const raw = structuredClone(configPayload?.raw || {});
    for (const input of $$('[data-config]')) {
      const path = input.dataset.config;
      if (path === 'provider.apiKey') {
        const value = input.value.trim();
        if (value) setByPath(raw, path, value);
        else deleteByPath(raw, path);
        continue;
      }
      if (input.type === 'checkbox') {
        setByPath(raw, path, input.checked);
        continue;
      }
      const text = input.value.trim();
      if (text === '') {
        deleteByPath(raw, path);
        continue;
      }
      if (input.dataset.number === 'true') {
        const value = Number(text);
        if (!Number.isFinite(value)) throw new Error(`${path} must be a number`);
        setByPath(raw, path, value);
      } else {
        setByPath(raw, path, text);
      }
    }
    for (const textarea of $$('[data-config-list]')) {
      const path = textarea.dataset.configList;
      const lines = textarea.value.split('\n').map((line) => line.trim()).filter(Boolean);
      if (lines.length === 0) deleteByPath(raw, path);
      else setByPath(raw, path, lines);
    }
    const roots = [];
    for (const row of $$('#roots-list .root-row')) {
      const value = $('input', row).value.trim();
      const access = $('select', row).value;
      if (value) roots.push({ path: value, access });
    }
    if (roots.length === 0) deleteByPath(raw, 'tools.files.roots');
    else setByPath(raw, 'tools.files.roots', roots);
    return raw;
  }

  async function saveSettings(raw) {
    setStatus($('#settings-status'), 'Saving…', '');
    try {
      const payload = await api('/api/config', { method: 'PUT', body: { config: raw } });
      fillSettings(payload);
      setStatus($('#settings-status'), 'Settings saved. The running agent was reloaded.', 'ok');
      showToast('Settings saved. The running agent was reloaded.', 'ok');
      await loadStatus();
    } catch (error) {
      setStatus($('#settings-status'), `Could not save: ${error.message}`, 'error');
      showToast(`Could not save: ${error.message}`, 'error');
    }
  }

  async function loadSettings({ notify = false } = {}) {
    try {
      const payload = await api('/api/config');
      fillSettings(payload);
      if (payload.parseError) {
        setStatus($('#settings-status'), payload.parseError, 'error');
        if (notify) showToast(payload.parseError, 'error');
      } else {
        setStatus($('#settings-status'), '', '');
        if (notify) showToast('Settings reloaded from disk.', 'ok');
      }
    } catch (error) {
      setStatus($('#settings-status'), error.message, 'error');
      if (notify) showToast(error.message, 'error');
    }
  }

  $('#settings-form').addEventListener('submit', (event) => {
    event.preventDefault();
    let raw;
    try {
      raw = collectSettings();
    } catch (error) {
      setStatus($('#settings-status'), error.message, 'error');
      showToast(error.message, 'error');
      return;
    }
    saveSettings(raw);
  });

  $('#settings-reload').addEventListener('click', () => loadSettings({ notify: true }));
  $('#add-root').addEventListener('click', () => addRootRow());

  $('#raw-save').addEventListener('click', () => {
    let parsed;
    try {
      parsed = JSON.parse($('#raw-config').value);
    } catch (error) {
      setStatus($('#settings-status'), `Raw JSON is not valid: ${error.message}`, 'error');
      showToast(`Raw JSON is not valid: ${error.message}`, 'error');
      return;
    }
    saveSettings(parsed);
  });

  // ===== Memory ===============================================================

  let memoryTab = 'facts';
  let transcriptId = null;

  function loadMemoryView() {
    if (memoryTab === 'facts') loadFacts();
    if (memoryTab === 'sessions') loadMemorySessions();
    if (memoryTab === 'audit') loadAudit();
  }

  for (const button of $$('.subtab')) {
    button.addEventListener('click', () => {
      memoryTab = button.dataset.memory;
      for (const other of $$('.subtab')) {
        if (other === button) other.setAttribute('aria-current', 'true');
        else other.removeAttribute('aria-current');
      }
      $('#memory-facts').hidden = memoryTab !== 'facts';
      $('#memory-sessions').hidden = memoryTab !== 'sessions';
      $('#memory-audit').hidden = memoryTab !== 'audit';
      loadMemoryView();
    });
  }

  function showEmptyNote(node, text) {
    node.hidden = false;
    node.textContent = text;
  }

  async function loadFacts() {
    const body = $('#facts-body');
    const empty = $('#facts-empty');
    try {
      const data = await api('/api/facts');
      body.textContent = '';
      empty.hidden = data.facts.length > 0;
      empty.textContent = 'No facts saved yet. The agent adds them with the remember tool, or you can add one above.';
      for (const fact of data.facts) {
        const row = document.createElement('tr');
        row.append(el('td', 'mono', fact.key));
        row.append(el('td', 'cell-value', fact.value));
        row.append(el('td', 'cell-time', formatTime(fact.at)));
        const actions = el('td', 'table-actions');
        const remove = el('button', 'btn btn-small btn-ghost', 'Delete');
        remove.type = 'button';
        remove.addEventListener('click', async () => {
          remove.disabled = true;
          try {
            await api(`/api/facts/${encodeURIComponent(fact.key)}`, { method: 'DELETE' });
            await loadFacts();
          } catch (error) {
            remove.disabled = false;
            setStatus($('#settings-status'), error.message, 'error');
          }
        });
        actions.append(remove);
        row.append(actions);
        body.append(row);
      }
    } catch (error) {
      body.textContent = '';
      showEmptyNote(empty, `Could not load facts: ${error.message}`);
    }
  }

  $('#fact-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const key = $('#fact-key').value.trim();
    const value = $('#fact-value').value.trim();
    if (!key || !value) return;
    try {
      await api('/api/facts', { method: 'PUT', body: { key, value } });
      $('#fact-key').value = '';
      $('#fact-value').value = '';
      await loadFacts();
    } catch (error) {
      showEmptyNote($('#facts-empty'), `Could not save the fact: ${error.message}`);
    }
  });

  async function loadMemorySessions() {
    const list = $('#memory-session-list');
    try {
      const data = await api('/api/sessions');
      list.textContent = '';
      if (!data.sessions.length) {
        const item = document.createElement('li');
        item.append(el('p', 'empty-note', data.memoryEnabled ? 'No saved sessions.' : 'Memory is disabled.'));
        list.append(item);
        return;
      }
      for (const session of data.sessions) {
        const item = document.createElement('li');
        const button = el('button', 'session-open');
        button.type = 'button';
        button.append(el('span', 'session-title', session.title || '(empty)'));
        button.append(el('span', 'session-meta', `${session.id} · ${session.messages} msgs · ${formatTime(session.updatedAt)}`));
        button.addEventListener('click', () => {
          loadTranscript(session.id).catch((error) => {
            $('#transcript').textContent = '';
            $('#transcript').append(el('p', 'empty-note', `Could not load the transcript: ${error.message}`));
          });
        });
        const remove = el('button', 'btn btn-small btn-ghost session-delete', 'Delete');
        remove.type = 'button';
        remove.addEventListener('click', () => deleteMemorySession(session.id));
        item.append(button, remove);
        list.append(item);
      }
    } catch (error) {
      list.textContent = '';
      const item = document.createElement('li');
      item.append(el('p', 'empty-note', `Could not load sessions: ${error.message}`));
      list.append(item);
    }
  }

  async function deleteMemorySession(id) {
    if (!confirm(`Delete session ${id}? This removes the stored conversation from disk.`)) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
      showToast('Session deleted.', 'ok');
      if (chat.sessionId === id) {
        chat.sessionId = null;
        chat.assistantEl = null;
        chat.pendingTools = [];
        chat.contextSize = null;
        $('#messages').textContent = '';
        $('#chat-title').textContent = 'Chat';
        renderContextGauge();
        addNotice('This session was deleted. The next message starts a new conversation.');
      }
      if (transcriptId === id) {
        transcriptId = null;
        $('#transcript').textContent = '';
        $('#transcript').append(el('p', 'empty-note', 'Pick a session to read its transcript.'));
      }
      await loadMemorySessions();
      await loadSessionList();
    } catch (error) {
      showToast(`Could not delete: ${error.message}`, 'error');
    }
  }

  async function loadTranscript(id) {
    const data = await api(`/api/sessions/${encodeURIComponent(id)}`);
    transcriptId = data.id;
    const container = $('#transcript');
    container.textContent = '';
    const head = el('div', 'transcript-head');
    head.append(el('span', '', id));
    container.append(head);
    if (!data.messages.length) {
      container.append(el('p', 'empty-note', 'No messages in this session.'));
      return;
    }
    for (const message of data.messages) {
      const article = el('article', `msg msg-${message.role}`);
      article.append(el('div', 'msg-role', `${message.role === 'user' ? 'you' : 'basicagentpy'} · ${formatTime(message.ts)}`));
      article.append(el('div', 'msg-text', message.content));
      container.append(article);
    }
  }

  async function loadAudit() {
    if (!status) await loadStatus().catch(() => {});
    const body = $('#audit-body');
    const empty = $('#audit-empty');
    const toolSelect = $('#audit-tool');
    if (toolSelect.options.length <= 1 && status?.tools) {
      for (const name of status.tools) {
        const option = document.createElement('option');
        option.value = name;
        option.textContent = name;
        toolSelect.append(option);
      }
    }
    const params = new URLSearchParams({ limit: '200' });
    if (toolSelect.value) params.set('tool', toolSelect.value);
    const ok = $('#audit-ok').value;
    if (ok) params.set('ok', ok);
    try {
      const data = await api(`/api/audit?${params}`);
      body.textContent = '';
      empty.hidden = data.entries.length > 0;
      empty.textContent = 'No audit entries.';
      $('#audit-count').textContent = `${data.entries.length} of ${data.total}`;
      for (const entry of data.entries) {
        const row = document.createElement('tr');
        row.append(el('td', 'cell-time', formatTime(entry.ts)));
        row.append(el('td', 'mono', entry.tool));
        const resultCell = document.createElement('td');
        resultCell.append(el('span', `chip ${entry.ok ? 'chip-ok' : 'chip-error'}`, entry.ok ? 'ok' : 'failed'));
        row.append(resultCell);
        row.append(el('td', 'cell-time', `${entry.durationMs} ms`));
        const detailsCell = document.createElement('td');
        const details = document.createElement('details');
        details.append(el('summary', '', 'view'));
        const payload = { args: entry.args ?? null };
        if (entry.error) payload.error = entry.error;
        if (entry.approvals?.length) payload.approvals = entry.approvals;
        details.append(el('pre', '', JSON.stringify(payload, null, 2)));
        detailsCell.append(details);
        row.append(detailsCell);
        body.append(row);
      }
    } catch (error) {
      body.textContent = '';
      showEmptyNote(empty, `Could not load the audit log: ${error.message}`);
    }
  }

  $('#audit-tool').addEventListener('change', () => loadAudit());
  $('#audit-ok').addEventListener('change', () => loadAudit());
  $('#audit-refresh').addEventListener('click', () => loadAudit());

  // ===== Boot =================================================================

  async function initChat() {
    await loadStatus();
    if (status.lastSessionId) await openSession(status.lastSessionId);
    else await newSession();
  }

  async function boot() {
    const initial = location.hash.slice(1);
    if (!token) {
      showTokenGate();
      showView(VIEWS.includes(initial) ? initial : 'chat');
      return;
    }
    showView(VIEWS.includes(initial) ? initial : 'chat');
    try {
      await initChat();
    } catch (error) {
      if (error.message !== 'unauthorized') addNotice(`error: ${error.message}`, 'error');
    }
  }

  boot();
})();
