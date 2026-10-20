'use strict';
// Loaded by WorkBuddy's native extension host. No model, subprocess, approval,
// account, cancel, arbitrary RPC or cloud-conversation endpoint is exposed.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

exports.activate = async function (wb) {
  const root = path.resolve(__dirname, '..');
  const endpoint = path.join(root, 'endpoint.json');
  const token = crypto.randomBytes(32).toString('hex');
  const jobs = new Map();
  const busy = new Set();
  const call = async (method, ...args) => {
    const value = await wb.invoke(method === 'local.list' ? 'wb:conversations.local:list' : 'wb:conversations:' + method, ...args);
    if (value?.errorCode) throw new Error(value.errorCode + ': ' + value.message);
    return value;
  };
  const metadata = info => ({native_session_id: info.id, title: info.title || '未命名对话',
    cwd: info.space?.cwd || info.cwd || '', updated_at: info.lastActivityAt,
    running: !['idle', 'new', 'created'].includes(info.state), transport: info.transport});
  const get = async id => {
    if (typeof id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(id)) throw new Error('原生会话 ID 无效。');
    const value = await call('get', id);
    if (!value?.info || value.info.id !== id || value.info.transport !== 'local') throw new Error('找不到所选 WorkBuddy 本机原生对话。');
    return value.info;
  };
  const dispatch = async q => {
    switch (q.method) {
      case 'health': return {protocol: 1, agent: 'workbuddy', pid: process.pid, host_pid: process.ppid};
      case 'list': {
        const result = await call('local.list', {page: q.page || 1, size: 50});
        return {sessions: (result.items || []).filter(i => i.transport === 'local').map(metadata),
          next_cursor: (q.page || 1) * 50 < result.total ? String((q.page || 1) + 1) : null};
      }
      case 'get': return metadata(await get(q.session_id));
      case 'create': {
        if (typeof q.cwd !== 'string' || !path.isAbsolute(q.cwd) || !fs.statSync(q.cwd).isDirectory()) throw new Error('新对话工作目录无效。');
        const result = await call('create', {cwd: q.cwd, transport: 'local'});
        if (!result?.info || result.info.transport !== 'local') throw new Error('WorkBuddy 未建立本机原生对话。');
        if (q.title) await call('rename', result.info.id, String(q.title).slice(0, 100));
        return metadata(await get(result.info.id));
      }
      case 'prompt': {
        if (typeof q.request_id !== 'string' || !/^[a-f0-9-]{36}$/.test(q.request_id)) throw new Error('通信消息 ID 无效。');
        if (typeof q.content !== 'string' || !q.content.trim() || q.content.length > 100000) throw new Error('通信内容无效。');
        const previous = jobs.get(q.request_id);
        if (previous) {
          if (previous.session_id !== q.session_id || previous.digest !== crypto.createHash('sha256').update(q.content).digest('hex')) throw new Error('消息 ID 被重复用于不同投递。');
          return {request_id: q.request_id, state: previous.state};
        }
        const info = await get(q.session_id);
        if (metadata(info).running || busy.has(q.session_id)) throw new Error('WorkBuddy 原对话正在工作，请等待它完成；未中断、重发或插入消息。');
        const job = {session_id: q.session_id, state: 'pending', digest: crypto.createHash('sha256').update(q.content).digest('hex')};
        jobs.set(q.request_id, job);
        busy.add(q.session_id);
        // No timeoutMs: a transport timeout must never cancel native work.
        call('runPrompt', q.session_id, [{type: 'text', text: q.content}], {clientRequestId: q.request_id})
          .then(result => {
            job.state = 'done';
            job.native_state = result.state;
            // Forward only public text. Thinking/tools/artifacts never leave this host.
            // Native content contains commentary before tool calls as well as the
            // final answer. Only the last public text block is the transport answer.
            job.text = ((result.content || []).filter(b => b.type === 'text' && typeof b.text === 'string').at(-1)?.text || '').slice(0, 100000);
          }, () => {job.state = 'failed'; job.error = 'WorkBuddy 原生轮次失败，请查看原软件。';})
          .finally(() => busy.delete(q.session_id));
        return {request_id: q.request_id, state: 'pending'};
      }
      case 'poll': {
        const job = jobs.get(q.request_id);
        if (!job || job.session_id !== q.session_id) throw new Error('投递观察已丢失；保留原会话，禁止自动重跑旧消息。');
        return {request_id: q.request_id, state: job.state, native_state: job.native_state,
          text: job.text, error: job.error};
      }
      default: throw new Error('不支持的连接器操作。');
    }
  };
  const server = http.createServer(async (req, res) => {
    const reply = (status, value) => {res.writeHead(status, {'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store'}); res.end(JSON.stringify(value));};
    const supplied = String(req.headers.authorization || '');
    const expected = 'Bearer ' + token;
    if (req.headers.origin || req.method !== 'POST' || req.url !== '/bridge' ||
        req.headers.host !== '127.0.0.1:' + server.address().port ||
        supplied.length !== expected.length || !crypto.timingSafeEqual(Buffer.from(supplied), Buffer.from(expected))) return reply(403, {error: '连接器认证失败。'});
    const chunks = [];
    let size = 0;
    try {
      for await (const chunk of req) {
        chunks.push(chunk);
        size += chunk.length;
        if (size > 400000) return reply(413, {error: '请求过大。'});
      }
      const value = await dispatch(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      reply(200, {result: value});
    } catch (error) {reply(409, {error: String(error.message).slice(0, 500)});}
  });
  await new Promise((resolve, reject) => {server.once('error', reject); server.listen(0, '127.0.0.1', resolve);});
  fs.writeFileSync(endpoint, JSON.stringify({protocol:1, pid:process.pid, host_pid:process.ppid, port:server.address().port, token}), {mode:0o600});
  return {dispose: async () => {
    server.close();
    try {if (JSON.parse(fs.readFileSync(endpoint, 'utf8')).token === token) fs.unlinkSync(endpoint);} catch {}
    // Native WorkBuddy owns running prompts. Never cancel them on bridge disconnection.
  }};
};
