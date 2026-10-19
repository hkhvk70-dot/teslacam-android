// Offline state-machine tests: no browser, network, credentials or device access.
// Run: node diagnostics/test_teslacam_ui_v41.cjs
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
// Exclude only automatic startup; keep the real functions and event bindings.
const code = script.split('$("version").textContent')[0];
assert.notEqual(code, script, 'expected startup boundary');
const KEYS = ['2026-10-20_01-02-03', '2026-10-20_01-03-03', '2026-10-20_01-04-03'];

function makeIndex(keys = KEYS) {
  const minutes = keys.map(key => ({
    key,
    clips: Object.fromEntries(['front', 'back', 'left_repeater', 'right_repeater']
      .map(cam => [cam, `TeslaCam/RecentClips/${key}-${cam}.mp4`]))
  }));
  return {events: [], minutes, live: minutes.at(-1)};
}

function harness(index = makeIndex()) {
  let sequence = 0, playCount = 0;
  const timers = new Map(), requests = [], nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      textContent: '', hidden: false, dataset: {}, children: [], attributes: {}, parentNode: null,
      paused: true, ended: false, duration: 60, currentTime: 0, readyState: 4, error: null, src: '',
      classList: {toggle() {}},
      setAttribute(key, value) { this.attributes[key] = value; },
      append(...children) { this.children.push(...children); for (const child of children) child.parentNode = this; },
      replaceChildren(...children) { this.children = children; for (const child of children) child.parentNode = this; },
      remove() {},
      removeAttribute(key) { if (key === 'src') this.src = ''; },
      load() {},
      pause() { this.paused = true; this.onpause?.(); },
      play() {
        this.paused = false;
        this.ended = false;
        playCount++;
        this.onplay?.();
        return Promise.resolve();
      }
    });
    return nodes.get(id);
  }
  const document = {
    hidden: false, baseURI: 'https://example.invalid/teslacam/', getElementById: node,
    createElement: () => node('created' + (++sequence)),
    createElementNS: () => node('created' + (++sequence)), addEventListener() {}
  };
  const context = vm.createContext({
    document, window: {addEventListener() {}}, URL, AbortController, Intl, Date, Number, Math, console,
    setTimeout(fn, delay) {
      const id = ++sequence;
      timers.set(id, {fn, delay});
      return id;
    },
    clearTimeout: id => timers.delete(id), setInterval() {},
    fetch: (url, options) => new Promise((resolve, reject) => {
      requests.push({url: String(url), options, resolve, reject});
      options.signal?.addEventListener('abort', () =>
        reject(Object.assign(new Error('aborted'), {name: 'AbortError'})));
    })
  });
  vm.runInContext(code, context);
  vm.runInContext('state.index=' + JSON.stringify(index), context);
  return {
    document, node, context, requests, timers, plays: () => playCount,
    run: source => vm.runInContext(source, context),
    reply(requestIndex, status = 200, data = {}) {
      assert.ok(requests[requestIndex], 'expected request ' + requestIndex);
      requests[requestIndex].resolve({status, ok: status >= 200 && status < 300, json: async () => data});
    },
    timer(delay) {
      const entry = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, 'expected timer ' + delay);
      timers.delete(entry[0]);
      entry[1].fn();
    }
  };
}

async function flush() { for (let i = 0; i < 8; i++) await Promise.resolve(); }

// v4.1 uses event-scoped 15-second segments. No network or device access.
function fixture(){const index=makeIndex();index.events=[{id:'SentryClips/2026-10-20_01-04-03',kind:'SentryClips',name:KEYS[2],status:'complete',clip_map:Object.fromEntries(index.minutes.map(m=>[m.key,Object.fromEntries(Object.entries(m.clips).map(([c,p])=>[c,p.replace('RecentClips','SentryClips/'+KEYS[2])]))])),proxies:{},mosaic:null,mosaic_map:[],cams:['front','back','left_repeater','right_repeater']}];return index;}
function finish(h,i,extra={}){const u=new URL(h.requests[i].url);h.reply(i,200,{status:'ready',url:'live-cache/segment'+i+'.mp4',key:u.searchParams.get('minute'),view:u.searchParams.get('view'),part:Number(u.searchParams.get('part')),offset:Number(u.searchParams.get('part'))*15,next_part:1,...(u.searchParams.get('event')?{event:u.searchParams.get('event')}:{}) ,...extra});}
const passed=[];
function check(name,fn){fn();passed.push(name);console.log('PASS',name);}
(async()=>{
{
 const h=harness();h.run('startLive('+JSON.stringify(KEYS[0])+')');
 check('foreground starts part zero',()=>{const u=new URL(h.requests[0].url);assert.equal(u.searchParams.get('part'),'0');assert.equal(u.searchParams.get('priority'),'foreground');});
 finish(h,0);await flush();
 check('prefetch next part before newer minute',()=>{const u=new URL(h.requests[1].url);assert.equal(u.searchParams.get('minute'),KEYS[0]);assert.equal(u.searchParams.get('part'),'1');assert.equal(u.searchParams.get('priority'),'prefetch');});
 finish(h,1,{next_part:2});await flush();h.node('player').ended=true;h.node('player').paused=true;h.node('player').onended();
 check('ready promotion avoids repeat request',()=>{assert.equal(h.run('state.media.part'),1);assert.equal(h.run('state.media.offset'),15);assert.equal(h.node('player').src,'live-cache/segment1.mp4');assert.equal(h.requests.length,3);});
 h.node('player').currentTime=2;h.run('updateCapture()');check('capture includes raw-minute offset',()=>assert.match(h.node('captureTime').textContent,/01:02:20/));
}
{
 const h=harness(fixture());h.run('state.mode="events";openEvent('+JSON.stringify(fixture().events[0].id)+')');
 check('uncached event queues scoped compression',()=>{const u=new URL(h.requests[0].url);assert.equal(u.searchParams.get('event'),fixture().events[0].id);assert.equal(u.searchParams.get('view'),'quad');assert.equal(u.searchParams.get('part'),'0');});
 check('event status distinguishes source and proxy readiness',()=>assert.match(h.run('eventState(currentEvent()).text'),/原片可用/));finish(h,0);await flush();
 check('event prefetch retains event and minute',()=>{const u=new URL(h.requests[1].url);assert.equal(u.searchParams.get('event'),fixture().events[0].id);assert.equal(u.searchParams.get('minute'),KEYS[0]);assert.equal(u.searchParams.get('part'),'1');});
 h.reply(1,202,{status:'processing',retry_after:2});await flush();const prior=h.run('state.prefetch'),timer=h.run('state.prefetch.timer');h.node('player').ended=true;h.node('player').paused=true;h.node('player').onended();
 check('queued event promotion retains job and timer',()=>{assert.equal(h.run('state.activeJob'),prior);assert.equal(h.run('state.activeJob.timer'),timer);assert.equal(h.requests.length,2);});
 h.timer(2000);finish(h,2,{next_part:null});await flush();check('last event part prepares next event minute',()=>{const u=new URL(h.requests[3].url);assert.equal(u.searchParams.get('minute'),KEYS[1]);assert.equal(u.searchParams.get('part'),'0');});
}
{
 const index=fixture(),e=index.events[0];e.proxies.front='_proxy/example/front.mp4';e.mosaic_map=[{key:KEYS[0],start:0,dur:60}];const h=harness(index);h.run('state.mode="events";openEvent('+JSON.stringify(e.id)+')');
 check('ready single proxy preferred without mosaic',()=>{assert.match(h.node('player').src,/front.mp4$/);assert.equal(h.requests.length,0);});
}
{
 const h=harness();h.run('startLive('+JSON.stringify(KEYS[2])+',{cached:true})');
 check('cold lookup requests cached-only endpoint',()=>{const u=new URL(h.requests[0].url);assert.equal(u.searchParams.get('cached'),'1');assert.equal(u.searchParams.get('part'),null);});
 h.reply(0,200,{status:'ready',url:'live-cache/cached.mp4',key:KEYS[1],view:'quad',part:2,offset:30,next_part:3});await flush();
 check('cached playback keeps actual time then prepares latest',()=>{assert.equal(h.run('state.liveKey'),KEYS[1]);assert.equal(h.run('state.media.offset'),30);const u=new URL(h.requests[1].url);assert.equal(u.searchParams.get('minute'),KEYS[2]);assert.equal(u.searchParams.get('part'),'0');});
}
{
 const h=harness();h.run('startLive('+JSON.stringify(KEYS[0])+');state.follow=false');finish(h,0);await flush();check('manual history continues same-minute parts',()=>assert.equal(new URL(h.requests[1].url).searchParams.get('part'),'1'));
 h.node('player').pause();check('manual pause cancels prefetch',()=>{assert.equal(h.run('state.prefetch'),null);assert.equal(h.run('advanceSegment()'),false);assert.equal(h.requests[1].options.signal.aborted,true);});
}
{
 const h=harness(fixture());h.run('state.mode="events";openEvent('+JSON.stringify(fixture().events[0].id)+')');h.run('backToEvents()');finish(h,0);await flush();
 check('stale event response cannot replace event list',()=>{assert.equal(h.node('player').src,'');assert.equal(h.run('state.eventId'),null);});
}
{
 const h=harness();h.run('startLive('+JSON.stringify(KEYS[0])+')');for(let i=0;i<4;i++){h.timer(15000);await flush();if(i<3){h.timer(2**(i+1)*1000);await flush();}}
 check('network retries bounded at three retries',()=>{assert.equal(h.requests.length,4);assert.equal(h.run('state.activeJob'),null);});
}
{
 const h=harness();h.run('startLive('+JSON.stringify(KEYS[2])+',{cached:true});state.liveView="front";startLive('+JSON.stringify(KEYS[0])+')');h.reply(0,200,{status:'ready',url:'live-cache/late.mp4',key:KEYS[2],view:'quad',part:0,offset:0,next_part:1});await flush();
 check('late cache response cannot override manual view',()=>{assert.equal(h.node('player').src,'');assert.equal(h.run('state.liveView'),'front');assert.equal(h.requests.length,2);assert.equal(new URL(h.requests[1].url).searchParams.get('view'),'front');});
}
{
 const h=harness();h.run('startLive('+JSON.stringify(KEYS[2])+',{cached:true})');h.reply(0,404,{status:'unavailable'});await flush();check('cache miss queues exactly one part zero',()=>{assert.equal(h.requests.length,2);assert.equal(new URL(h.requests[1].url).searchParams.get('part'),'0');});
}
{
 const h=harness(fixture());h.run('state.mode="events";openEvent('+JSON.stringify(fixture().events[0].id)+')');finish(h,0);await flush();h.node('player').duration=NaN;h.node('player').onloadedmetadata();
 check('invalid event segment duration invokes bounded retry',()=>{assert.match(h.node('playMessage').textContent,/正在重试/);assert.equal(h.run('state.videoFailure.attempts'),1);});
}
{
 const h=harness(fixture());h.run('state.mode="events";openEvent('+JSON.stringify(fixture().events[0].id)+')');finish(h,0,{part:1,offset:15,next_part:2});await flush();check('wrong response part rejected',()=>{assert.equal(h.node('player').src,'');assert.match(h.node('playMessage').textContent,/不匹配/);});
}
{
 const h=harness(fixture());h.run('state.mode="events";openEvent('+JSON.stringify(fixture().events[0].id)+')');h.document.hidden=true;h.run('suspendPage()');h.document.hidden=false;h.run('resumePage()');await flush();
 check('loading event resumes after background',()=>{assert.equal(h.requests.length,3);assert.equal(new URL(h.requests[2].url).searchParams.get('event'),fixture().events[0].id);});
}
for(const eventMode of [false,true]){
 const h=harness(fixture());if(eventMode)h.run('state.mode="events";state.eventId='+JSON.stringify(fixture().events[0].id)+';state.eventMinute='+JSON.stringify(KEYS[0])+';playEvent({part:2})');else h.run('startLive('+JSON.stringify(KEYS[0])+',{part:2})');
 h.reply(0,503,{status:'error',message:'busy'});await flush();h.node('retryButton').onclick();
 check((eventMode?'event':'live')+' manual retry keeps pending part two',()=>{const u=new URL(h.requests[1].url);assert.equal(u.searchParams.get('part'),'2');assert.equal(u.searchParams.get('minute'),KEYS[0]);assert.equal(u.searchParams.get('event'),eventMode?fixture().events[0].id:null);});
}
for(const eventMode of [false,true]){
 const h=harness(fixture());if(eventMode)h.run('state.mode="events";state.eventId='+JSON.stringify(fixture().events[0].id)+';state.eventMinute='+JSON.stringify(KEYS[0])+';playEvent({part:2})');else h.run('startLive('+JSON.stringify(KEYS[0])+',{part:2})');
 h.reply(0,202,{status:'processing',retry_after:2});await flush();h.document.hidden=true;h.run('suspendPage()');h.document.hidden=false;h.run('resumePage()');await flush();
 check((eventMode?'event':'live')+' resume keeps pending part two',()=>{const u=new URL(h.requests[2].url);assert.equal(u.searchParams.get('part'),'2');assert.equal(u.searchParams.get('minute'),KEYS[0]);assert.equal(u.searchParams.get('event'),eventMode?fixture().events[0].id:null);});
}
{
 const h=harness();h.run('startLive('+JSON.stringify(KEYS[0])+')');finish(h,0,{next_part:1});await flush();finish(h,1,{next_part:2});await flush();h.node('player').ended=true;h.node('player').onended();h.reply(2,202,{status:'processing',retry_after:2});await flush();h.node('player').ended=true;h.node('player').onended();
 check('queued promoted part two saves foreground target',()=>{assert.equal(h.run('state.media'),null);assert.equal(h.run('state.segmentTarget.part'),2);});
 h.node('retryButton').onclick();check('retry after queued promotion keeps part two',()=>assert.equal(new URL(h.requests.at(-1).url).searchParams.get('part'),'2'));
}
console.log('TOTAL',passed.length,'PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
