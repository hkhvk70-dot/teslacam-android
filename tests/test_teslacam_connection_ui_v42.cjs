// Offline tests for the real connection panel; no credentials, network or device access.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'..','web','index.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1].split('$("version").textContent')[0];
function harness(){
  const nodes=new Map(),requests=[],timers=new Map();let next=0;
  const node=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',hidden:false,dataset:{},attributes:{},children:[],paused:true,ended:false,currentTime:0,duration:60,readyState:4,error:null,src:'',parentNode:null,classList:{toggle(){}},setAttribute(k,v){this.attributes[k]=v},removeAttribute(k){if(k==='src')this.src=''},append(...xs){this.children.push(...xs);for(const x of xs)x.parentNode=this},replaceChildren(...xs){this.children=xs},pause(){this.paused=true;this.onpause?.()},load(){},play(){this.paused=false;return Promise.resolve()}});return nodes.get(id)};
  const document={hidden:false,baseURI:'https://example.invalid/teslacam/',getElementById:node,createElement:()=>node('new'+(++next)),createElementNS:()=>node('new'+(++next)),addEventListener(){}};
  const context=vm.createContext({document,window:{addEventListener(){}},URL,AbortController,Intl,Date,Number,Math,Object,console,setTimeout(fn,delay){const id=++next;timers.set(id,{fn,delay});return id},clearTimeout:id=>timers.delete(id),setInterval(){},fetch(url,options){return new Promise((resolve,reject)=>{requests.push({url:String(url),options,resolve,reject});options.signal?.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'})))})}});
  vm.runInContext(script,context);
  return {document,node,requests,timers,run:s=>vm.runInContext(s,context),reply(i,status=200,data={}){requests[i].resolve({status,ok:status>=200&&status<300,json:async()=>data})},timer(delay){const entry=[...timers].find(([,t])=>t.delay===delay);assert.ok(entry,'timer '+delay);timers.delete(entry[0]);entry[1].fn()}};
}
async function flush(){for(let n=0;n<10;n++)await Promise.resolve()}
let count=0;function check(name,fn){fn();count++;console.log('PASS',name)}
(async()=>{
  check('version is 4.2 and expandable panel exists',()=>{assert.match(html,/APP_VERSION = "4\.2"/);assert.match(html,/<details class="route-info"/);assert.match(html,/私网地址不等于已确认 P2P 直连/)});
  {
    const h=harness();h.run('refreshConnection()');check('excluded startup adds no extra requests to existing state tests',()=>assert.equal(h.requests.length,0));
    h.run('routeState.enabled=true;refreshConnection();refreshConnection()');
    check('independent status request is same origin and deduplicated',()=>{assert.equal(h.requests.length,1);assert.equal(h.requests[0].url,'api/connection');assert.equal(h.requests[0].options.credentials,'same-origin');assert.equal(h.requests[0].options.cache,'no-store')});
    h.reply(0,200,{access:'tailnet',peer_path:'unknown',observed_at:'2026-10-20T04:05:06+08:00'});await flush();
    check('tailnet explicitly stays unverified',()=>{assert.equal(h.node('routeLabel').textContent,'Tailscale 私网');assert.equal(h.node('routeNote').textContent,'直连／中继未核验');assert.match(h.node('routeObserved').textContent,/04:05:06/)});
    h.run('refreshConnection()');h.reply(1,503,{});await flush();check('failed check clears previously known entry and timestamp',()=>{assert.equal(h.node('routeLabel').textContent,'连接方式未确认');assert.equal(h.node('routeObserved').textContent,'')});
  }
  for(const [access,label] of [['forwarded','转发访问'],['lan','局域网访问'],['external','连接方式未确认'],['unknown','连接方式未确认']]){
    const h=harness();h.run('routeState.enabled=true;refreshConnection()');h.reply(0,200,{access,peer_path:'unknown'});await flush();check('renders '+access+' without inventing P2P',()=>{assert.equal(h.node('routeLabel').textContent,label);assert.doesNotMatch(h.node('routeNote').textContent,/已确认|P2P/)});
  }
  for(const data of [null,{}, {access:'<script>alert(1)</script>'}]){
    const h=harness();h.run('routeState.enabled=true;renderRoute("forwarded");refreshConnection()');h.reply(0,200,data);await flush();check('invalid payload falls back to unknown '+JSON.stringify(data),()=>assert.equal(h.node('routeLabel').textContent,'连接方式未确认'));
  }
  {
    const h=harness();h.run('routeState.enabled=true;renderRoute("tailnet");refreshConnection()');h.timer(5000);await flush();check('five second timeout aborts and removes old status',()=>{assert.equal(h.requests[0].options.signal.aborted,true);assert.equal(h.node('routeLabel').textContent,'连接方式未确认');assert.equal(h.run('routeState.controller'),null)});
  }
  {
    const h=harness();h.run('routeState.enabled=true;renderRoute("forwarded");refreshConnection()');h.document.hidden=true;h.run('suspendPage()');await flush();h.reply(0,200,{access:'tailnet'});await flush();
    check('background aborts, clears label and ignores stale result',()=>{assert.equal(h.requests[0].options.signal.aborted,true);assert.equal(h.node('routeLabel').textContent,'连接方式未确认')});h.run('refreshConnection()');check('hidden page makes no status request',()=>assert.equal(h.requests.length,1));
    h.document.hidden=false;h.run('resumePage()');check('resume starts index before optional status',()=>{assert.equal(h.requests.length,2);assert.match(h.requests[1].url,/^index.json/)});h.reply(1,200,{minutes:[],events:[]});await flush();check('index completion starts fresh status on resume',()=>{assert.equal(h.requests.length,3);assert.equal(h.requests[2].url,'api/connection')});
    h.reply(2,200,{access:'lan'});await flush();check('new result replaces unknown after resume',()=>assert.equal(h.node('routeLabel').textContent,'局域网访问'));
    h.timer(8000);check('eight second refresh still starts only index first',()=>{assert.equal(h.requests.length,4);assert.match(h.requests[3].url,/^index.json/)});h.reply(3,200,{minutes:[],events:[]});await flush();check('periodic refresh checks connection independently',()=>{assert.equal(h.requests.length,5);assert.equal(h.requests[4].url,'api/connection');assert.equal(h.run('state.indexFlight'),false)});
  }
  {
    const h=harness();h.run('routeState.enabled=true;refreshConnection();refreshIndex()');check('pending connection request does not block index',()=>{assert.equal(h.requests.length,2);assert.match(h.requests[1].url,/^index.json/)});h.reply(1,200,{minutes:[],events:[]});await flush();check('index renders normally while connection pending',()=>{assert.equal(h.node('connectionText').textContent,'已连接');assert.equal(h.run('state.indexFlight'),false);assert.equal(h.requests.length,2)});
  }
  console.log(`${count} connection UI checks passed`);
})().catch(error=>{console.error(error);process.exitCode=1});
