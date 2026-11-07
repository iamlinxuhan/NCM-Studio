// ==UserScript==
// @name         NCM Studio · 网易云请求分析钩子
// @namespace    https://github.com/iamlinxuhan/ncmctl-GUI
// @version      1.0.0
// @description  记录 music.163.com 网页端的接口请求，解密 eapi 请求体，并把请求和刚才点击的按钮对应起来。用于分析评论、收藏、播客、漫游、关注等功能的真实接口。
// @author       iamlinxuhan
// @match        https://music.163.com/*
// @match        https://*.music.163.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

/*
 * 用法
 * ----
 * 1. 浏览器装 Tampermonkey，新建脚本，把本文件全文粘进去保存；
 * 2. 打开 https://music.163.com/ ，右下角会出现「NCM Hook」小按钮；
 * 3. 点开面板，然后在页面上正常点「收藏」「评论」「播客」「漫游」「关注」；
 * 4. 每次点击后 3 秒内发出的接口会被打上按钮文字标签；
 * 5. 点「复制」把 JSON 拿走，或点「下载」存成文件。
 *
 * 说明
 * ----
 * - eapi 请求体用固定密钥 e82ckenh8dichen8 做 AES-128-ECB 解密，能还原出真实
 *   API 路径、业务参数和 header；
 * - weapi 的外层密钥是每次随机的，没有 RSA 私钥无法离线解密，脚本只能记录
 *   密文长度；这类请求请看「响应」和接口路径；
 * - 老版网页端如果暴露了 window.asrsea，脚本会顺手记录它加密前的明文；
 * - 所有数据只留在本页面内存里，不会发到任何地方。导出的 JSON 含请求和响应，
 *   可能包含账号信息，不要公开分享。
 */

(function () {
  'use strict';

  if (window.__ncmHookInstalled) return;
  window.__ncmHookInstalled = true;

  // ===================== crypto begin =====================
  // 仅用于 eapi 请求体的离线解密，不参与任何请求签名。

  const EAPI_KEY_TEXT = 'e82ckenh8dichen8';
  const EAPI_SEP = '-36cd479b6b5-';

  function gfMul(a, b) {
    let p = 0;
    for (let i = 0; i < 8; i++) {
      if (b & 1) p ^= a;
      const high = a & 0x80;
      a = (a << 1) & 0xff;
      if (high) a ^= 0x1b;
      b >>= 1;
    }
    return p;
  }

  function gfPow(a, n) {
    let r = 1;
    while (n) {
      if (n & 1) r = gfMul(r, a);
      a = gfMul(a, a);
      n >>= 1;
    }
    return r;
  }

  // S 盒按定义算出来，省得贴 512 个魔法数字
  const SBOX = new Uint8Array(256);
  const INV_SBOX = new Uint8Array(256);
  (function buildSbox() {
    for (let i = 0; i < 256; i++) {
      const inv = i ? gfPow(i, 254) : 0;
      let s = inv;
      for (let k = 1; k <= 4; k++) {
        s ^= ((inv << k) | (inv >> (8 - k))) & 0xff;
      }
      s ^= 0x63;
      SBOX[i] = s;
      INV_SBOX[s] = i;
    }
  })();

  const MUL9 = new Uint8Array(256);
  const MUL11 = new Uint8Array(256);
  const MUL13 = new Uint8Array(256);
  const MUL14 = new Uint8Array(256);
  for (let i = 0; i < 256; i++) {
    MUL9[i] = gfMul(i, 9);
    MUL11[i] = gfMul(i, 11);
    MUL13[i] = gfMul(i, 13);
    MUL14[i] = gfMul(i, 14);
  }

  function expandKey(key) {
    if (key.length !== 16) throw new Error('只支持 AES-128');
    const w = new Uint8Array(176);
    w.set(key);
    let rcon = 1;
    for (let i = 16; i < 176; i += 4) {
      let t0 = w[i - 4];
      let t1 = w[i - 3];
      let t2 = w[i - 2];
      let t3 = w[i - 1];
      if (i % 16 === 0) {
        const tmp = t0;
        t0 = SBOX[t1] ^ rcon;
        t1 = SBOX[t2];
        t2 = SBOX[t3];
        t3 = SBOX[tmp];
        rcon = ((rcon << 1) ^ ((rcon & 0x80) ? 0x1b : 0)) & 0xff;
      }
      w[i] = w[i - 16] ^ t0;
      w[i + 1] = w[i - 15] ^ t1;
      w[i + 2] = w[i - 14] ^ t2;
      w[i + 3] = w[i - 13] ^ t3;
    }
    return w;
  }

  function invShiftRows(s) {
    // 第 1 行右移 1
    let t = s[13];
    s[13] = s[9]; s[9] = s[5]; s[5] = s[1]; s[1] = t;
    // 第 2 行右移 2
    t = s[2]; s[2] = s[10]; s[10] = t;
    t = s[6]; s[6] = s[14]; s[14] = t;
    // 第 3 行右移 3（等于左移 1）
    t = s[3]; s[3] = s[7]; s[7] = s[11]; s[11] = s[15]; s[15] = t;
  }

  function invSubBytes(s) {
    for (let i = 0; i < 16; i++) s[i] = INV_SBOX[s[i]];
  }

  function addRoundKey(s, w, offset) {
    for (let i = 0; i < 16; i++) s[i] ^= w[offset + i];
  }

  function invMixColumns(s) {
    for (let c = 0; c < 16; c += 4) {
      const a0 = s[c], a1 = s[c + 1], a2 = s[c + 2], a3 = s[c + 3];
      s[c] = MUL14[a0] ^ MUL11[a1] ^ MUL13[a2] ^ MUL9[a3];
      s[c + 1] = MUL9[a0] ^ MUL14[a1] ^ MUL11[a2] ^ MUL13[a3];
      s[c + 2] = MUL13[a0] ^ MUL9[a1] ^ MUL14[a2] ^ MUL11[a3];
      s[c + 3] = MUL11[a0] ^ MUL13[a1] ^ MUL9[a2] ^ MUL14[a3];
    }
  }

  function decryptBlock(w, block) {
    const s = Uint8Array.from(block);
    addRoundKey(s, w, 160);
    for (let round = 9; round >= 1; round--) {
      invShiftRows(s);
      invSubBytes(s);
      addRoundKey(s, w, round * 16);
      invMixColumns(s);
    }
    invShiftRows(s);
    invSubBytes(s);
    addRoundKey(s, w, 0);
    return s;
  }

  function ecbDecrypt(bytes, key) {
    if (!bytes.length || bytes.length % 16 !== 0) {
      throw new Error('密文长度不是 16 的倍数');
    }
    const w = expandKey(key);
    const out = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i += 16) {
      out.set(decryptBlock(w, bytes.subarray(i, i + 16)), i);
    }
    // 去掉 PKCS#7；填充不合法就按原文返回，至少不吞数据
    const pad = out[out.length - 1];
    if (pad >= 1 && pad <= 16 && pad <= out.length) {
      let ok = true;
      for (let i = out.length - pad; i < out.length; i++) {
        if (out[i] !== pad) { ok = false; break; }
      }
      if (ok) return out.subarray(0, out.length - pad);
    }
    return out;
  }

  function hexToBytes(hex) {
    const clean = String(hex).trim();
    if (!/^[0-9a-fA-F]+$/.test(clean) || clean.length % 2) return null;
    const out = new Uint8Array(clean.length / 2);
    for (let i = 0; i < out.length; i++) {
      out[i] = parseInt(clean.substr(i * 2, 2), 16);
    }
    return out;
  }

  const EAPI_KEY = new TextEncoder().encode(EAPI_KEY_TEXT);

  /** 解开 eapi 的 params，返回 {url, payload, header, digest, raw}。解不开返回 null。 */
  function decodeEapiParams(hex) {
    try {
      const bytes = hexToBytes(hex);
      if (!bytes) return null;
      const plain = new TextDecoder().decode(ecbDecrypt(bytes, EAPI_KEY));
      const parts = plain.split(EAPI_SEP);
      if (parts.length < 3) return { raw: plain };
      let payload = parts[1];
      let header = null;
      try {
        payload = JSON.parse(parts[1]);
        if (payload && typeof payload.header === 'string') {
          try { header = JSON.parse(payload.header); } catch (e) { header = payload.header; }
        } else if (payload) {
          header = payload.header || null;
        }
      } catch (e) {
        // 不是 JSON 就保留原文
      }
      return { url: parts[0], payload, header, digest: parts[2] };
    } catch (e) {
      return null;
    }
  }

  // ===================== crypto end =====================

  // ---------------------------------------------------------------- 状态

  const MAX_ENTRIES = 500;
  const TAG_WINDOW = 3000;
  const API_RE = /\/(api|weapi|eapi)\//i;
  const entries = [];
  let paused = false;
  let lastClick = null;

  function now() { return Date.now(); }

  function timeText(ts) {
    const d = new Date(ts);
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  function truncate(value, limit) {
    if (typeof value !== 'string') {
      try { value = JSON.stringify(value); } catch (e) { value = String(value); }
    }
    if (value.length <= limit) return value;
    return value.slice(0, limit) + `\n…（截断，共 ${value.length} 字符）`;
  }

  function resolveUrl(url) {
    try { return new URL(String(url), location.href); } catch (e) { return null; }
  }

  function currentTag() {
    if (!lastClick) return '';
    if (now() - lastClick.at > TAG_WINDOW) return '';
    return lastClick.text;
  }

  /** 把请求体拆成可读结构；eapi 顺手解密。 */
  function parseRequestBody(body) {
    if (body == null) return { kind: 'empty' };
    if (typeof body !== 'string') {
      if (typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams) {
        body = body.toString();
      } else if (typeof FormData !== 'undefined' && body instanceof FormData) {
        const pairs = {};
        try { body.forEach((v, k) => { pairs[k] = String(v); }); } catch (e) {}
        return { kind: 'formdata', data: pairs };
      } else {
        return { kind: 'other', data: truncate(String(body), 2000) };
      }
    }
    const text = body.trim();
    if (!text) return { kind: 'empty' };

    // JSON 请求（少数接口）
    if (text.startsWith('{') || text.startsWith('[')) {
      try { return { kind: 'json', data: JSON.parse(text) }; } catch (e) {}
    }

    let params;
    try { params = new URLSearchParams(text); } catch (e) { return { kind: 'raw', data: truncate(text, 4000) }; }

    if (params.has('encSecKey')) {
      const encrypted = params.get('params') || '';
      return {
        kind: 'weapi',
        data: {
          note: 'weapi 外层密钥随机，离线解不开；请看响应和接口路径',
          paramsLength: encrypted.length,
          encSecKeyLength: (params.get('encSecKey') || '').length,
        },
      };
    }

    if (params.has('params')) {
      const hex = params.get('params') || '';
      const decoded = decodeEapiParams(hex);
      if (decoded) {
        return {
          kind: 'eapi',
          data: {
            api: decoded.url || '',
            payload: decoded.payload,
            header: decoded.header,
            digest: decoded.digest,
          },
        };
      }
      return { kind: 'eapi-undecoded', data: { paramsLength: hex.length } };
    }

    const data = {};
    for (const [k, v] of params) data[k] = v;
    return { kind: 'form', data };
  }

  function parseResponse(text) {
    if (!text) return { data: '', note: '空响应' };
    try {
      const json = JSON.parse(text);
      if (json && typeof json === 'object') {
        const code = json.code;
        return { data: json, code: typeof code === 'number' ? code : undefined };
      }
      return { data: json };
    } catch (e) {
      return { data: truncate(text, 4000) };
    }
  }

  function addEntry(entry) {
    if (paused) return;
    entries.push(entry);
    if (entries.length > MAX_ENTRIES) entries.splice(0, entries.length - MAX_ENTRIES);
    render();
  }

  function makeEntry(method, url, body, status, responseText, extra) {
    const resolved = resolveUrl(url);
    if (!resolved) return null;
    const path = resolved.pathname + resolved.search;
    if (!API_RE.test(resolved.pathname)) return null;

    const request = parseRequestBody(body);
    const response = parseResponse(responseText);
    return Object.assign({
      t: now(),
      method: String(method || 'GET').toUpperCase(),
      url: resolved.href,
      path: resolved.pathname,
      query: resolved.search ? Object.fromEntries(resolved.searchParams) : {},
      api: request.kind === 'eapi' ? request.data.api : '',
      kind: request.kind,
      tag: currentTag(),
      request: request.data,
      status: status,
      code: response.code,
      response: response.data,
    }, extra || {});
  }

  // ---------------------------------------------------------------- 钩子

  const originalFetch = window.fetch;
  if (typeof originalFetch === 'function') {
    window.fetch = function (input, init) {
      const started = now();
      let method = 'GET';
      let url = '';
      let body = null;
      try {
        if (typeof Request !== 'undefined' && input instanceof Request) {
          method = input.method || 'GET';
          url = input.url;
        } else {
          url = String(input);
        }
        if (init && init.method) method = init.method;
        if (init && init.body != null) body = init.body;
      } catch (e) {}

      const promise = originalFetch.apply(this, arguments);
      promise.then((response) => {
        try {
          const clone = response.clone();
          clone.text().then((text) => {
            const entry = makeEntry(method, url, body, response.status, text, {
              duration: now() - started,
            });
            if (entry) addEntry(entry);
          }).catch(() => {});
        } catch (e) {}
      }).catch(() => {});
      return promise;
    };
  }

  const XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype) {
    const originalOpen = XHR.prototype.open;
    const originalSend = XHR.prototype.send;

    XHR.prototype.open = function (method, url) {
      try { this.__ncmMeta = { method: method, url: url }; } catch (e) {}
      return originalOpen.apply(this, arguments);
    };

    XHR.prototype.send = function (body) {
      const meta = this.__ncmMeta;
      if (!meta) return originalSend.apply(this, arguments);
      const started = now();
      try {
        this.addEventListener('loadend', () => {
          let text = '';
          try {
            if (this.responseType === '' || this.responseType === 'text') {
              text = this.responseText;
            } else if (this.responseType === 'json') {
              text = JSON.stringify(this.response);
            }
          } catch (e) {}
          const status = this.status;
          const entry = makeEntry(meta.method, meta.url, body, status, text, {
            duration: now() - started,
          });
          if (entry) addEntry(entry);
        });
      } catch (e) {}
      return originalSend.apply(this, arguments);
    };
  }

  // 播放日志一类会走 sendBeacon
  if (navigator.sendBeacon) {
    const originalBeacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function (url, data) {
      try {
        const entry = makeEntry('BEACON', url, data, 0, '', {});
        if (entry) addEntry(entry);
      } catch (e) {}
      return originalBeacon(url, data);
    };
  }

  // 老版网页端暴露的加密入口：能直接拿到加密前的明文
  let asrseaTries = 0;
  const asrseaTimer = setInterval(() => {
    try {
      if (typeof window.asrsea === 'function' && !window.asrsea.__ncmWrapped) {
        const original = window.asrsea;
        const wrapped = function () {
          try {
            const plain = arguments[0];
            if (typeof plain === 'string' && !paused) {
              let data = plain;
              try { data = JSON.parse(plain); } catch (e) {}
              addEntry({
                t: now(),
                method: 'ASRSEA',
                url: location.href,
                path: '(window.asrsea)',
                api: '',
                kind: 'asrsea',
                tag: currentTag(),
                request: data,
                status: 0,
                code: undefined,
                response: '（这是加密前的明文，实际请求见相邻记录）',
              });
            }
          } catch (e) {}
          return original.apply(this, arguments);
        };
        wrapped.__ncmWrapped = true;
        window.asrsea = wrapped;
        clearInterval(asrseaTimer);
      }
    } catch (e) {}
    if (++asrseaTries > 100) clearInterval(asrseaTimer);
  }, 200);

  // 点击标注：记录刚才点的是什么，给 3 秒内的请求打标签
  document.addEventListener('click', (event) => {
    try {
      let node = event.target;
      for (let depth = 0; depth < 6 && node && node.nodeType === 1; depth++, node = node.parentElement) {
        const label = (node.getAttribute('aria-label') || node.getAttribute('title') || '').trim();
        const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
        const chosen = label || (text.length <= 24 ? text : '');
        if (chosen) {
          lastClick = { text: chosen, at: now() };
          return;
        }
      }
    } catch (e) {}
  }, true);

  // ---------------------------------------------------------------- 面板

  let host = null;
  let panel = null;
  let listEl = null;
  let filterEl = null;
  let statusEl = null;
  let visible = false;

  const STYLE = `
    :host { all: initial; }
    .wrap {
      position: fixed; right: 14px; bottom: 14px; z-index: 2147483647;
      font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      color: #e6edf3;
    }
    .pill {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 6px 10px; border-radius: 999px; cursor: pointer;
      background: #1f6feb; color: #fff; box-shadow: 0 4px 14px rgba(0,0,0,.28);
      user-select: none;
    }
    .panel {
      width: 560px; height: 400px; display: none; flex-direction: column;
      background: #0d1117; border: 1px solid #30363d; border-radius: 10px;
      box-shadow: 0 12px 40px rgba(0,0,0,.5); overflow: hidden; margin-bottom: 8px;
    }
    .panel.open { display: flex; }
    .bar { display: flex; align-items: center; gap: 6px; padding: 8px; border-bottom: 1px solid #30363d; flex-wrap: wrap; }
    .bar .title { font-weight: 600; margin-right: auto; }
    button {
      font: inherit; color: #e6edf3; background: #21262d; border: 1px solid #30363d;
      border-radius: 6px; padding: 3px 8px; cursor: pointer;
    }
    button:hover { background: #30363d; }
    input[type=text] {
      font: inherit; color: #e6edf3; background: #010409; border: 1px solid #30363d;
      border-radius: 6px; padding: 3px 8px; width: 150px;
    }
    .list { flex: 1; overflow: auto; }
    .row { padding: 5px 8px; border-bottom: 1px solid #161b22; cursor: pointer; }
    .row:hover { background: #161b22; }
    .row.open { background: #161b22; }
    .line { display: flex; gap: 6px; align-items: baseline; white-space: nowrap; overflow: hidden; }
    .t { color: #7d8590; }
    .m { color: #79c0ff; font-weight: 600; }
    .m.POST { color: #ffa657; }
    .p { overflow: hidden; text-overflow: ellipsis; }
    .tag { color: #d2a8ff; background: #1f2937; border-radius: 4px; padding: 0 4px; }
    .s { margin-left: auto; color: #3fb950; }
    .s.bad { color: #f85149; }
    pre { margin: 6px 0 0; padding: 6px; background: #010409; border-radius: 6px; color: #c9d1d9; max-height: 220px; overflow: auto; white-space: pre-wrap; word-break: break-all; }
    .empty { color: #7d8590; padding: 12px; text-align: center; }
    .foot { padding: 4px 8px; color: #7d8590; border-top: 1px solid #30363d; }
  `;

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function buildPanel() {
    if (host) return;
    host = document.createElement('div');
    host.id = 'ncm-hook-host';
    const shadow = host.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = STYLE;

    const wrap = document.createElement('div');
    wrap.className = 'wrap';

    panel = document.createElement('div');
    panel.className = 'panel';

    const bar = document.createElement('div');
    bar.className = 'bar';
    bar.innerHTML = `
      <span class="title">NCM Hook <span class="count">0</span></span>
      <button data-act="pause">暂停</button>
      <button data-act="clear">清空</button>
      <button data-act="copy">复制</button>
      <button data-act="download">下载</button>
    `;
    filterEl = document.createElement('input');
    filterEl.type = 'text';
    filterEl.placeholder = '过滤 URL / 标签';
    filterEl.addEventListener('input', () => render());
    bar.appendChild(filterEl);

    listEl = document.createElement('div');
    listEl.className = 'list';

    statusEl = document.createElement('div');
    statusEl.className = 'foot';
    statusEl.textContent = '数据只在本页内存；导出文件可能含账号信息，别公开分享。';

    panel.appendChild(bar);
    panel.appendChild(listEl);
    panel.appendChild(statusEl);

    const pill = document.createElement('div');
    pill.className = 'pill';
    pill.innerHTML = '<span>●</span> NCM Hook <span class="count">0</span>';
    pill.addEventListener('click', () => {
      visible = !visible;
      panel.classList.toggle('open', visible);
      if (visible) render();
    });

    wrap.appendChild(panel);
    wrap.appendChild(pill);
    shadow.appendChild(style);
    shadow.appendChild(wrap);
    (document.body || document.documentElement).appendChild(host);

    bar.addEventListener('click', (event) => {
      const act = event.target && event.target.getAttribute && event.target.getAttribute('data-act');
      if (!act) return;
      if (act === 'pause') {
        paused = !paused;
        event.target.textContent = paused ? '继续' : '暂停';
        updateStatus();
      } else if (act === 'clear') {
        entries.length = 0;
        render();
      } else if (act === 'copy') {
        copyAll();
      } else if (act === 'download') {
        downloadAll();
      }
    });
  }

  function updateStatus() {
    if (!statusEl) return;
    statusEl.textContent = paused
      ? '已暂停记录。点「继续」恢复。'
      : '数据只在本页内存；导出文件可能含账号信息，别公开分享。';
  }

  function matchFilter(entry) {
    const keyword = (filterEl && filterEl.value || '').trim().toLowerCase();
    if (!keyword) return true;
    const haystack = [
      entry.url, entry.path, entry.api, entry.tag, entry.kind,
      entry.request && JSON.stringify(entry.request),
      entry.response && JSON.stringify(entry.response),
    ].join(' ').toLowerCase();
    return haystack.includes(keyword);
  }

  function render() {
    if (!panel || !visible || !listEl) {
      updateCounts();
      return;
    }
    const fragment = document.createDocumentFragment();
    for (let i = entries.length - 1; i >= 0; i--) {
      const entry = entries[i];
      if (!matchFilter(entry)) continue;
      const row = document.createElement('div');
      row.className = 'row';
      row.dataset.index = String(i);

      const apiText = entry.api || entry.path;
      const line = document.createElement('div');
      line.className = 'line';
      line.innerHTML =
        `<span class="t">${timeText(entry.t)}</span>` +
        `<span class="m ${escapeHtml(entry.method)}">${escapeHtml(entry.method)}</span>` +
        `<span class="p">${escapeHtml(apiText)}</span>` +
        (entry.tag ? `<span class="tag">${escapeHtml(entry.tag)}</span>` : '') +
        `<span class="s">${entry.status || ''}${entry.code != null ? ' / ' + entry.code : ''}</span>`;
      row.appendChild(line);

      const body = document.createElement('pre');
      body.style.display = 'none';
      body.textContent = formatEntry(entry);
      row.appendChild(body);

      row.addEventListener('click', () => {
        const open = row.classList.toggle('open');
        body.style.display = open ? 'block' : 'none';
      });

      fragment.appendChild(row);
    }
    if (!fragment.childNodes.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = entries.length ? '没有匹配的记录' : '还没有捕获到请求。在页面上点一下「收藏」或「评论」试试。';
      fragment.appendChild(empty);
    }
    listEl.replaceChildren(fragment);
    updateCounts();
  }

  function formatEntry(entry) {
    const parts = [];
    parts.push(`${entry.method} ${entry.url}`);
    if (entry.api) parts.push(`真实接口: ${entry.api}`);
    if (entry.tag) parts.push(`触发按钮: ${entry.tag}`);
    if (entry.duration != null) parts.push(`耗时: ${entry.duration} ms`);
    parts.push('');
    parts.push('请求:');
    parts.push(truncate(entry.request, 8000));
    parts.push('');
    parts.push('响应:');
    parts.push(truncate(entry.response, 8000));
    return parts.join('\n');
  }

  function updateCounts() {
    if (!host) return;
    const count = host.shadowRoot ? host.shadowRoot.querySelectorAll('.count') : [];
    count.forEach((el) => { el.textContent = String(entries.length); });
  }

  function copyAll() {
    const text = JSON.stringify(exportData(), null, 2);
    const done = () => { statusEl.textContent = `已复制 ${entries.length} 条记录到剪贴板。`; };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
      } else {
        fallbackCopy(text, done);
      }
    } catch (e) {
      fallbackCopy(text, done);
    }
  }

  function fallbackCopy(text, done) {
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    try { document.execCommand('copy'); done(); } catch (e) {}
    area.remove();
  }

  function downloadAll() {
    const blob = new Blob([JSON.stringify(exportData(), null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    anchor.href = url;
    anchor.download = `ncm-hook-${stamp}.json`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function exportData() {
    return {
      capturedAt: new Date().toISOString(),
      page: location.href,
      userAgent: navigator.userAgent,
      count: entries.length,
      entries: entries.slice(),
    };
  }

  // 控制台也能用
  window.__ncmHook = {
    entries,
    export: exportData,
    clear() { entries.length = 0; render(); },
    pause(value) { paused = Boolean(value); render(); },
    decodeEapiParams,
  };

  function boot() {
    buildPanel();
    render();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
