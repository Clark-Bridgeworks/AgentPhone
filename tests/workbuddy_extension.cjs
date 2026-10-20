'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');

async function main() {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'agentphone-wb-unit-'));
  fs.mkdirSync(path.join(temporary, 'server'));
  fs.copyFileSync(path.resolve(__dirname, '../src/agentphone/desktop/native/workbuddy-bridge/server/index.cjs'), path.join(temporary, 'server/index.cjs'));
  const calls = [];
  let state = 'idle';
  let finish;
  const fixture = {invoke: async (channel, ...args) => {
    calls.push({channel,args});
    if(channel==='wb:conversations:get') return {info:{id:args[0], title:'原对话',state,transport:'local',space:{cwd:temporary}}};
    if(channel==='wb:conversations.local:list') return {items:[{id:'native-fixture',title:'原对话',state:'idle',transport:'local'}],total:1};
    if(channel==='wb:conversations:runPrompt') return new Promise(resolve=>{finish=resolve;});
    throw Error('unexpected channel');
  }};
  const extension = await require(path.join(temporary,'server/index.cjs')).activate(fixture);
  const endpoint = JSON.parse(fs.readFileSync(path.join(temporary,'endpoint.json'),'utf8'));
  const rpc = (q, headers={}, split=false) => new Promise((resolve,reject)=>{
    const request=http.request({hostname:'127.0.0.1',port:endpoint.port,path:'/bridge',method:'POST',
      headers:{Authorization:'Bearer '+endpoint.token,'Content-Type':'application/json',...headers}},response=>{
        let body='';response.setEncoding('utf8');response.on('data',chunk=>body+=chunk);
        response.on('end',()=>resolve({status:response.statusCode,...JSON.parse(body)}));
      });request.on('error',reject);
    const body=Buffer.from(JSON.stringify(q));
    if(split) for(const byte of body) request.write(Buffer.from([byte])); else request.write(body);
    request.end();
  });
  try {
    assert.equal((await rpc({method:'health'},{Origin:'http://evil.example'})).status,403);
    assert.equal((await rpc({method:'health'},{Host:'evil.example'})).status,403);
    assert.equal((await rpc({method:'health'},{Authorization:'Bearer '+crypto.randomBytes(32).toString('hex')})).status,403);
    assert.equal((await rpc({method:'cancel'})).status,409);
    assert.equal((await rpc({method:'list'})).result.sessions[0].title,'原对话');
    const request_id=crypto.randomUUID();
    const message={method:'prompt',session_id:'native-fixture',request_id,content:'中文天体运动😀'};
    assert.equal((await rpc(message,{},true)).result.state,'pending');
    assert.equal(calls.find(c=>c.channel==='wb:conversations:runPrompt').args[1][0].text,message.content);
    assert.equal((await rpc(message)).status,200);
    assert.equal(calls.filter(c=>c.channel==='wb:conversations:runPrompt').length,1);
    assert.equal((await rpc({...message,content:'different'})).status,409);
    assert.equal((await rpc({...message,request_id:crypto.randomUUID()})).status,409);
    const final='{"action":"wait"}';
    finish({state:'completed',content:[{type:'text',text:'public commentary'},{type:'reasoning',text:'must never forward'},
      {type:'tool',content:'must never forward'},{type:'text',text:final}]});
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal((await rpc({method:'poll',session_id:'native-fixture',request_id})).result.text,final);
    assert.equal((await rpc({method:'poll',session_id:'wrong-native',request_id})).status,409);
    state='working';
    assert.equal((await rpc({...message,request_id:crypto.randomUUID()})).status,409);
    assert(!calls.some(c=>/cancel|resolvePending|configSet|account/.test(c.channel)));
    assert.equal(calls.find(c=>c.channel==='wb:conversations:runPrompt').args[2].timeoutMs,undefined);
    console.log('WorkBuddy extension: authentication, UTF-8, native identity, idempotency, busy guard, final text and no cancellation passed (fixture).');
  } finally {
    await extension.dispose();
    fs.rmSync(temporary,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
