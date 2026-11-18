'use strict';
const $ = (selector, parent = document) => parent.querySelector(selector);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (value !== false && value != null) node.setAttribute(key, value === true ? '' : String(value));
  }
  node.append(...children.flat().filter(c => c != null));
  return node;
};
const button = (text, callback, cls = '') => el('button', {type:'button', class:cls, onclick:callback}, text);
const state = {csrf:'', route:0, reader:null, selection:null, activeJobs:new Map(), jobObservers:new Map(), vocabWaits:new Map(), sheet:0, sheetWait:null};
const view = $('#app');
const readingId=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');
function syncVisualViewport() {
  const viewport=window.visualViewport;
  document.documentElement.style.setProperty('--visual-height',(viewport?.height||innerHeight)+'px');
  document.documentElement.style.setProperty('--visual-top',(viewport?.offsetTop||0)+'px');
  const compact=matchMedia('(max-width:600px), (pointer:coarse) and (max-width:1000px) and (max-height:500px)').matches;
  $('#sheet').classList.toggle('keyboard-open',compact&&viewport?.height<innerHeight*.78);
}
window.visualViewport?.addEventListener('resize',syncVisualViewport,{passive:true});
window.visualViewport?.addEventListener('scroll',syncVisualViewport,{passive:true});
window.addEventListener('resize',syncVisualViewport,{passive:true});syncVisualViewport();
function readingViewport() {
  const viewport=window.visualViewport,style=getComputedStyle(document.documentElement);
  const inset=name=>parseFloat(style.getPropertyValue('--safe-'+name))||0;
  return {left:(viewport?.offsetLeft||0)+inset('left'),top:(viewport?.offsetTop||0)+inset('top'),
    width:(viewport?.width||innerWidth)-inset('left')-inset('right'),
    height:(viewport?.height||innerHeight)-inset('top')-inset('bottom')};
}
// Older installed reading extensions also listen on document. Keep our reader and
// dialog mouse events local so one gesture cannot start another model request.
// selectionchange still reaches document for our own selection toolbar.
for (const host of [view, $('#sheet')]) {
  for (const type of ['dblclick', 'mouseup']) host.addEventListener(type, event => event.stopPropagation());
}
let toastTimer, progressTimer, readerScroll, progressFrame;
function toast(text) {
  $('#toast').textContent = text; $('#toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 4500);
}
async function networkApi(path, {method = 'GET', data, form, keepalive = false, uploadSize, etag='', metadata=false} = {}) {
  const headers = {};
  if(etag)headers['If-None-Match']=etag;
  if(uploadSize!==undefined)headers['X-Upload-Size']=String(uploadSize);
  if (method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  if (data !== undefined) headers['Content-Type'] = 'application/json';
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20_000);
  let response,result;
  try{
    response = await fetch('/api' + path, {method, headers, credentials:'same-origin',signal:controller.signal,
      body:form || (data !== undefined ? JSON.stringify(data) : undefined), keepalive});
    if(response.status===304)return {unchanged:true};
    result = await response.json();
  }finally{clearTimeout(timer);}
  if (!response.ok) {
    if (response.status === 401 && path !== '/login') {state.csrf='';cacheEpoch++;cacheRequests.clear();localReading.clear();ReadingSync.reset();ReadingCache.clear();showLogin();}
    const error=new Error(result.error || '请求未完成');error.code=result.code;error.status=response.status;
    if(error.code==='config_locked'&&location.hash==='#/settings')queueMicrotask(route);
    throw error;
  }
  return metadata?{value:result,etag:response.headers.get('ETag')||''}:result;
}
const cacheRequests=new Map();let cacheEpoch=0;
const cacheable=path=>/^\/(library|reading-stats)(\?|$)/.test(path)||/^\/books\/[^/]+(?:\/articles\/\d+(?:\/vocab)?)?$/.test(path);
const articleGroup=path=>path.match(/^\/books\/[^/]+\/articles\/\d+/)?.[0]||'';
function cacheComparable(path,value){
  if(articleGroup(path)===path){const {history,readingTiming,state,...content}=value;return JSON.stringify(content);}
  return JSON.stringify(value);
}
function cacheNotice(message,refresh=true){
  if(!state.csrf)return;let notice=$('#reading-cache-notice');
  if(!notice){notice=el('div',{id:'reading-cache-notice',class:'reading-cache-notice',role:'status'});view.prepend(notice);}
  notice.replaceChildren(el('span',{},message),...(refresh?[button('刷新',()=>route({refresh:true}))]:[]));
}
async function cachedRead(path,{fresh=false,onUpdate}={}){
  fresh=fresh||state.forceReadingRefresh;
  const record=await ReadingCache.get(path),token=state.route,epoch=cacheEpoch;
  const refresh=()=>{
    if(!cacheRequests.has(path)){
      const pending=networkApi(path,{etag:record?.etag,metadata:true}).then(async response=>{
        const value=response.unchanged?record?.value:response.value;if(!value)return null;
        if(epoch!==cacheEpoch||!state.csrf)return value;
        await ReadingCache.put(path,value,{etag:response.etag||record?.etag,group:articleGroup(path)});
        if(epoch===cacheEpoch&&state.reader&&baseFor(state.reader)===path&&state.reader.state!==value.state){
          setReaderState(state.reader,value.state);
        }
        if(record&&token===state.route&&cacheComparable(path,record.value)!==cacheComparable(path,value)){
          if(onUpdate)onUpdate(value);else cacheNotice('服务器有更新，刷新后查看；当前阅读位置保持不变。');
        }
        return value;
      }).finally(()=>{if(cacheRequests.get(path)===pending)cacheRequests.delete(path);});
      cacheRequests.set(path,pending);
    }
    return cacheRequests.get(path);
  };
  if(record&&!fresh){
    if(navigator.onLine!==false&&(!path.endsWith('/vocab')||Date.now()-record.checked>30_000))refresh().catch(()=>{if(token===state.route)cacheNotice('正在使用本机缓存，网络恢复后会更新。',false);});
    else if(navigator.onLine===false)cacheNotice('正在使用本机缓存，网络恢复后会更新。',false);
    return record.value;
  }
  return refresh();
}
async function staleReadingCache(){
  cacheEpoch++;cacheRequests.clear();
  await ReadingCache.stale(key=>!key.startsWith('cover:'));
}
// Covers share the reading cache budget and logout cleanup. An image is fetched
// only near the viewport; a saved data URL also works during offline navigation.
const coverRequests=new Map();
function cachedCover(book){
  const img=el('img',{alt:book.title||book.fileName,loading:'lazy',decoding:'async'});
  const path=`/api/books/${encodeURIComponent(book.id)}/cover`,key='cover:'+book.id,epoch=cacheEpoch,flight=epoch+':'+key;
  async function load(){
    const record=await ReadingCache.get(key);
    if(epoch!==cacheEpoch||!state.csrf)return;
    if(record)img.src=record.value;
    if(navigator.onLine===false||record&&Date.now()-record.checked<3600_000)return;
    try{
      if(!coverRequests.has(flight)){
        const pending=(async()=>{
          const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20_000);
          try{
            const response=await fetch(path,{credentials:'same-origin',signal:controller.signal,
              headers:record?.etag?{'If-None-Match':record.etag}:{}});
            let value=record?.value;
            if(response.status!==304){
              if(!response.ok)return null;
              const blob=await response.blob();
              if(!blob.type.startsWith('image/')||blob.size>5*1024*1024)return null;
              value=await new Promise((resolve,reject)=>{const file=new FileReader();file.onload=()=>resolve(file.result);file.onerror=()=>reject(file.error);file.readAsDataURL(blob);});
            }
            if(value&&epoch===cacheEpoch&&state.csrf)await ReadingCache.put(key,value,{etag:response.headers.get('ETag')||record?.etag});
            return value;
          }finally{clearTimeout(timer);}
        })().finally(()=>{if(coverRequests.get(flight)===pending)coverRequests.delete(flight);});
        coverRequests.set(flight,pending);
      }
      const value=await coverRequests.get(flight);
      if(value&&epoch===cacheEpoch&&state.csrf)img.src=value;
    }catch{} // Keep a saved cover when a weak connection fails.
  }
  // One observer per render avoids retaining detached book cards after filtering.
  queueMicrotask(()=>{
    if(!img.isConnected)return;
    if(!('IntersectionObserver' in window)){load();return;}
    const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting)){observer.disconnect();load();}},{rootMargin:'300px'});
    observer.observe(img);img.coverObserver=observer;
  });
  return img;
}
async function api(path,options={}){
  if((options.method||'GET')==='GET'&&cacheable(path))return cachedRead(path,options);
  const value=await networkApi(path,options);
  if(options.method&&options.method!=='GET'&&(path==='/settings'||path==='/preferences')){
    state.modelRevision=String(Date.now());await ReadingCache.remove(key=>key.startsWith('analysis:')||key.startsWith('meaning:'));
  }
  if(options.method&&options.method!=='GET'&&!['/login','/logout'].includes(path)&&!path.startsWith('/settings/')){
    if(path==='/wordbook'||path.startsWith('/wordbook/'))await ReadingCache.remove(key=>key.startsWith('meaning:'));
    await staleReadingCache();
  }
  return value;
}

// Only reading positions and cumulative clock samples are queued. No tokens or
// general POST bodies are persisted. Journal writes are synchronous on pagehide.
const ReadingSync=(()=>{
  let items=[],journal='',prefix='',running=null,offset=0,enabled=false,epoch=0;
  const now=()=>Math.round(Date.now()+offset);
  function persist(){try{
    const own=items.filter(item=>item.journal===journal);
    if(own.length)localStorage.setItem(journal,JSON.stringify(own));else localStorage.removeItem(journal);
    return true;
  }catch{return false;}}
  function acknowledge(item){try{
    const remaining=JSON.parse(localStorage.getItem(item.journal)||'[]').filter(old=>old.id!==item.id);
    if(remaining.length)localStorage.setItem(item.journal,JSON.stringify(remaining));else localStorage.removeItem(item.journal);
  }catch{}}
  function init(identity){
    epoch++;enabled=true;offset=(identity.serverTime||Date.now())-Date.now();
    prefix='kandu-reading-outbox:'+identity.cacheScope+':';journal=prefix+readingId();items=[];
    try{for(const key of Object.keys(localStorage))if(key.startsWith(prefix)){
      const pending=JSON.parse(localStorage.getItem(key)||'[]').filter(item=>
        /^\/books\/[^/]+\/articles\/\d+\/(progress|reading-time)$/.test(item.path));
      items.push(...pending.map(item=>({...item,journal:key})));
    }}catch{}
    items.sort((a,b)=>a.at-b.at);
  }
  function reset(){epoch++;enabled=false;items=[];if(prefix)try{
    for(const key of Object.keys(localStorage))if(key.startsWith(prefix))localStorage.removeItem(key);
  }catch{}journal='';}
  function add(path,data){
    if(!enabled)return null;
    const item={id:readingId(),path,data,at:now(),journal};
    if(path.endsWith('/progress'))items=items.filter(old=>old.path!==path||old.journal!==journal);
    items.push(item);
    if(!persist())cacheNotice('浏览器存储已满，阅读记录暂存在本页，请保持页面打开以完成同步。',false);
    return item;
  }
  async function drain(){
    if(!enabled||!state.csrf||!items.length||navigator.onLine===false)return;
    if(running)return running;
    const owner=epoch;
    running=(async()=>{
      while(enabled&&owner===epoch&&items.length&&state.csrf){
        const item=items[0];let result;
        try{result=await networkApi(item.path,{method:'POST',data:item.data,keepalive:true});}
        catch(error){
          if(error.status===400||error.status===404){items=items.filter(i=>i.id!==item.id);acknowledge(item);persist();cacheNotice('有阅读记录因内容变化或超过七天未能同步，请检查最近阅读位置。',false);continue;}
          throw error;
        }
        if(owner!==epoch)return;
        items=items.filter(i=>i.id!==item.id);acknowledge(item);persist();
        window.dispatchEvent(new CustomEvent('reading-synced',{detail:{...item,result}}));
      }
    })().finally(()=>{running=null;});
    return running;
  }
  return {init,reset,add,drain,now,pending:()=>items.length,entries:()=>structuredClone(items)};
})();
async function initReadingStorage(identity){
  state.cacheIdentity=(state.cacheIdentity||0)+1;
  cacheEpoch++;cacheRequests.clear();
  state.modelRevision=identity.modelRevision||'';
  await ReadingCache.init(identity.cacheScope||'reading');ReadingSync.init(identity);
  ReadingSync.drain().catch(()=>{});
}
const cacheAuth=typeof BroadcastChannel==='function'?new BroadcastChannel('kandu-reading-auth'):null;
cacheAuth?.addEventListener('message',event=>{if(event.data==='logout'&&state.csrf){state.csrf='';cacheEpoch++;cacheRequests.clear();localReading.clear();ReadingSync.reset();ReadingCache.clear();showLogin();}});
window.addEventListener('online',()=>{ReadingSync.drain().catch(()=>{});if(state.reader)loadVocab(state.reader,{fresh:true}).catch(()=>{});});
setInterval(()=>{if(state.csrf)ReadingSync.drain().catch(()=>{});},15_000);
function action(callback) { return (...args) => { if(args[0]?.type==='submit')args[0].preventDefault();return Promise.resolve().then(() => callback(...args)).catch(error => toast(error.message)); }; }
function empty(title, text='') { return el('div', {class:'empty'}, el('h2', {}, title), el('p', {}, text)); }
function heading(kicker, title, subtitle, actions=[]) {
  return el('div', {class:'page-heading'}, el('div', {}, el('span', {class:'eyebrow'}, kicker), el('h1', {}, title),
    subtitle ? el('p', {class:'muted'}, subtitle) : null), el('div', {class:'row'}, ...actions));
}
function linkFor(bookId, index, paragraph=null) {
  return `#/read/${encodeURIComponent(bookId)}/${index}`+(Number.isInteger(paragraph)?`?paragraph=${paragraph}`:'');
}
function baseFor(reader=state.reader) { return `/books/${encodeURIComponent(reader.bookId)}/articles/${reader.index}`; }
let sheetCloseEvents=0;
function hideSheetDialog() {
  if($('#sheet').open){sheetCloseEvents++;$('#sheet').close();}
}
function clearSheetWait() {
  const wait=state.sheetWait;if(!wait)return;
  cancelAnimationFrame(wait.frame);
  for(const name of ['scroll','resize'])window.removeEventListener(name,wait.reposition);
  for(const name of ['scroll','resize'])window.visualViewport?.removeEventListener(name,wait.reposition);
  wait.card.remove();state.sheetWait=null;
  $('#sheet-background').hidden=true;
}
function closeSheet() {
  readingClockTick();state.sheet++;clearSheetWait();hideSheetDialog();readingClockTick();
}
function centerSheet() {
  const x=scrollX,y=scrollY;
  readingClockTick();
  if(!$('#sheet').open)$('#sheet').showModal();
  window.scrollTo({left:x,top:y,behavior:'instant'});
  readingClockTick();flushReadingClock();
}
function positionSheetWait() {
  const wait=state.sheetWait;if(!wait||wait.card.hidden)return;
  const {left,top,width,height}=readingViewport();
  const card=wait.card,body=$('#reader-body');if(!body)return;
  card.style.width=Math.min(272,width-24)+'px';
  const bounds=card.getBoundingClientRect(),prose=body.getBoundingClientRect();
  let x=left+width-bounds.width-12,y=top+height-bounds.height-12;
  if(x<prose.right+12) {
    if(prose.left-left>=bounds.width+24)x=left+12;
    else {
      let anchor=wait.range?.startContainer.isConnected?wait.range.getBoundingClientRect():null;
      if(!anchor||anchor.bottom<top||anchor.top>top+height) {
        const index=currentPosition(wait.reader)?.paragraphIndex??wait.paragraph;
        anchor=$(`#para-${index} p`)?.getBoundingClientRect();
      }
      // Narrow screens have no side gutter: choose the edge furthest from
      // the selected line. Keep the card compact and the prose interactive.
      if(anchor&&anchor.bottom>y&&anchor.top<top+height)y=top+12;
    }
  }
  card.style.left=x+'px';card.style.top=y+'px';
}
function dockSheet(sheet) {
  const wait=state.sheetWait;if(!wait||sheet!==state.sheet)return;
  const x=scrollX,y=scrollY,wasOpen=$('#sheet').open;
  readingClockTick();hideSheetDialog();wait.card.hidden=false;
  $('#sheet-background').hidden=true;
  if(wasOpen)view.focus({preventScroll:true});
  window.scrollTo({left:x,top:y,behavior:'instant'});
  positionSheetWait();readingClockTick();
}
function waitForSheet(sheet, reader, paragraph=null, status='正在准备请求…') {
  if(sheet!==state.sheet||state.reader!==reader)return;
  if(!state.sheetWait) {
    const selection=getSelection(),range=selection&&!selection.isCollapsed?selection.getRangeAt(0).cloneRange():reader.selectionRange?.cloneRange()||null;
    const title=el('strong',{},$('#sheet-title').textContent),label=el('p',{class:'sheet-wait-status','aria-live':'polite'},status);
    const card=el('aside',{id:'sheet-wait',role:'region','aria-label':'AI 请求进度'},
      el('div',{class:'sheet-wait-head'},el('span',{class:'spinner','aria-hidden':'true'}),title,
        button('×',closeSheet,'sheet-wait-close')),
      label,el('div',{class:'sheet-wait-footer'},el('small',{},'可继续阅读 · 完成后自动展开'),
        button('查看',()=>{if(sheet!==state.sheet)return;card.hidden=true;$('#sheet-background').hidden=false;centerSheet();},'text-button')));
    $('.sheet-wait-close',card).setAttribute('aria-label','关闭等待提示');
    $('.sheet-wait-close',card).title='关闭提示，已提交的任务继续在后台执行';
    const wait=state.sheetWait={sheet,reader,paragraph,range,card,label,frame:null};
    wait.reposition=()=>{if(!wait.frame)wait.frame=requestAnimationFrame(()=>{wait.frame=null;positionSheetWait();});};
    document.body.append(card);
    getSelection()?.removeAllRanges();
    for(const name of ['scroll','resize'])window.addEventListener(name,wait.reposition,{passive:true});
    for(const name of ['scroll','resize'])window.visualViewport?.addEventListener(name,wait.reposition,{passive:true});
  }
  state.sheetWait.label.textContent=status;dockSheet(sheet);
}
function updateSheetWait(sheet,status) {
  if(sheet===state.sheet&&state.sheetWait)state.sheetWait.label.textContent=status||'正在等待模型响应…';
}
function finishSheetWait(sheet) {
  if(sheet!==state.sheet)return false;
  if(state.sheetWait&&state.sheetWait.reader!==state.reader)return false;
  clearSheetWait();centerSheet();return true;
}
function showSheet(title, content, wait=null) {
  state.reader?.hoverHide?.();
  if(state.reader?.clock){readingClockTick(state.reader);flushReadingClock(state.reader);}
  const x=scrollX,y=scrollY;
  clearSheetWait();hideSheetDialog();
  state.sheet += 1;
  $('#sheet-title').textContent = title;
  $('#sheet-content').replaceChildren(content);
  if(wait)waitForSheet(state.sheet,wait.reader,wait.paragraph,wait.status);
  else centerSheet();
  window.scrollTo({left:x,top:y,behavior:'instant'});
  const selectionBar=$('#selection-tools');if(selectionBar)selectionBar.hidden=true;
  if(state.reader)clearReaderSelection(state.reader);
  return state.sheet;
}
$('#sheet-close').before(el('button',{id:'sheet-background',type:'button',hidden:true,onclick:()=>dockSheet(state.sheet)},'继续阅读'));
$('#sheet-close').onclick = closeSheet;
$('#sheet').addEventListener('cancel',event=>{event.preventDefault();closeSheet();});
$('#sheet').addEventListener('click', event => { if (event.target === $('#sheet')) { const r=$('#sheet').getBoundingClientRect(); if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom) closeSheet(); } });
$('#sheet').addEventListener('close', () => { if(sheetCloseEvents){sheetCloseEvents--;return;}state.sheet++;clearSheetWait(); });
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&state.sheetWait&&!$('#sheet').open){event.preventDefault();closeSheet();}});
$('#logout').onclick = action(async () => {
  if(state.reader){state.reader.hoverCleanup?.();saveProgress(true);stopReadingClock(state.reader);}
  if(ReadingSync.pending())await ReadingSync.drain();
  await api('/logout', {method:'POST', data:{}});state.csrf='';cacheEpoch++;cacheRequests.clear();cacheAuth?.postMessage('logout');ReadingSync.reset();localReading.clear();await ReadingCache.clear();state.reader=null;showLogin();
});

function showLogin() {
  state.cacheIdentity=(state.cacheIdentity||0)+1;
  state.route++;
  state.reader?.hoverCleanup?.();
  state.reader?.touchCleanup?.();
  if(state.reader)stopReadingClock(state.reader,false);
  state.reader=null;
  $('#navigation').hidden = true;
  closeSheet();
  view.replaceChildren($('#login-template').content.cloneNode(true));
  $('#login-form').onsubmit = async event => {
    event.preventDefault(); const submit=$('button', event.target); submit.disabled=true;
    try {
      const response=await api('/login', {method:'POST', data:{password:$('#password').value}});
      state.csrf=response.csrf;await initReadingStorage(response); await route();
    } catch(error) { $('#login-error').textContent=error.message; }
    finally { submit.disabled=false; }
  };
}

function displayJobBar() {
  $('#jobbar').hidden=state.activeJobs.size===0;
  if(state.activeJobs.size) $('#job-status').textContent=[...state.activeJobs.values()].map(j=>j.status).join(' · ');
}
async function runJob(path, data={}, onDone, onUpdate, onFailure) {
  const settled=path.endsWith('/generate-vocab')?pauseVocabularyGeneration(path):()=>{};
  try {
    const {jobId}=await api(path,{method:'POST',data});
    if(state.activeJobs.has(jobId))toast('这个任务已经在进行中');
    return watchJob(jobId,onDone,onUpdate,settled,onFailure);
  } catch(error) {settled();throw error;}
}
function watchJob(jobId,onDone,onUpdate,onSettled,onFailure) {
  if(!state.jobObservers.has(jobId))state.jobObservers.set(jobId,[]);
  state.jobObservers.get(jobId).push({onDone,onUpdate,onSettled,onFailure});
  if(state.activeJobs.has(jobId))return jobId;
  const started=Date.now(); state.activeJobs.set(jobId,{status:'任务已提交'}); displayJobBar();
  const poll=async()=>{
    try {
      const job=await api('/jobs/'+jobId);
      const elapsed=Math.round((Date.now()-started)/1000);
      state.activeJobs.set(jobId,{status:`${job.status} · ${elapsed} 秒`}); displayJobBar();
      for(const observer of state.jobObservers.get(jobId)||[])if(observer.onUpdate)observer.onUpdate(job);
      if(job.state==='done'||job.state==='stopped'||job.state==='error') {
        state.activeJobs.delete(jobId); displayJobBar();
        if(job.state==='error') toast(job.error);
        else if(job.state==='done') toast(job.result?.failed?'部分完成，可再次补缺':'已完成并保存');
        const observers=state.jobObservers.get(jobId)||[];state.jobObservers.delete(jobId);
        try {for(const observer of observers)if(observer.onDone)await observer.onDone(job);}
        finally {for(const observer of observers)if(observer.onSettled)observer.onSettled();}
        return;
      }
      setTimeout(poll,650);
    } catch(error) {
      const observers=state.jobObservers.get(jobId)||[];
      state.activeJobs.delete(jobId);state.jobObservers.delete(jobId);
      for(const observer of observers)if(observer.onSettled)observer.onSettled();
      for(const observer of observers)if(observer.onFailure)observer.onFailure(error);
      displayJobBar(); toast('连接中断，服务器任务仍会保存结果。'+error.message);
    }
  };
  poll(); return jobId;
}

async function library(token) {
  const [data,stats]=await Promise.all([api('/library'),api('/reading-stats')]); if(token!==state.route)return;
  for(const [path,history] of localReading){
    const match=path.match(/^\/books\/([^/]+)\/articles\/(\d+)$/);if(!match)continue;
    const bookId=decodeURIComponent(match[1]),articleIndex=Number(match[2]);
    const previous=data.recent.find(r=>r.bookId===bookId&&r.articleIndex===articleIndex);
    if(!previous||history.lastReadAt>=previous.lastReadAt){
      data.recent=data.recent.filter(r=>r!==previous);data.recent.push({...history,bookId,articleIndex});
    }
  }
  data.recent.sort((a,b)=>b.lastReadAt-a.lastReadAt);data.recent=data.recent.slice(0,80);
  const content=el('div');
  content.append(heading('A WINDOW TO THE WORLD','我的阅读室',`${data.books.length} 本刊物 · 阅读结果集中保存，各设备接续阅读`,[
    button('刊物来源',action(podcastSheet)),
    button('＋ 添加刊物',uploadSheet,'primary')
  ]));
  content.append(el('a',{class:'reading-milestone',href:'#/reading-stats'},
    el('div',{},el('span',{class:'eyebrow'},'EVERY ARTICLE COUNTS'),el('strong',{},`已读 ${stats.articleCount.toLocaleString()} 篇 · 累计 ${stats.totalWords.toLocaleString()} 词`)),
    el('span',{},'查看阅读足迹 →')));
  if(data.recent.length) {
    const list=el('div',{class:'recent-grid',id:'recent-reading'});
    let expanded=false;
    const more=button('更多 →',()=>{expanded=!expanded;renderRecent();},'text-button');
    more.setAttribute('aria-controls','recent-reading');
    more.classList.toggle('recent-more-desktop-empty',data.recent.length===3);
    const renderRecent=()=>{
      more.textContent=expanded?'收起 ↑':'更多 →';
      more.setAttribute('aria-expanded',String(expanded));
      more.hidden=data.recent.length<=2;
      list.classList.toggle('expanded',expanded);
      list.replaceChildren(...data.recent.slice(0,expanded?data.recent.length:3).map(r=>el('a',{class:'recent-card',href:linkFor(r.bookId,r.articleIndex)},
        el('small',{},r.bookTitle),el('h3',{},r.title),
        el('div',{class:'recent-bottom'},el('span',{},`第 ${r.paragraphIndex+1} 段 · ${Math.round(r.progress*100)}%`),el('span',{},'继续阅读 →')),
        el('div',{class:'progress-line'},el('progress',{max:1,value:r.progress})))));
    };
    content.append(el('div',{class:'section-title'},el('h2',{},'继续阅读'),more),list); renderRecent();
  }
  const search=el('input',{class:'search',type:'search',placeholder:'搜索刊名、期号…','aria-label':'搜索刊物'});
  const publications=[...new Set(data.books.map(b=>b.publication))];
  const select=el('select',{'aria-label':'按刊物筛选'},el('option',{value:''},'全部刊物'),...publications.map(p=>el('option',{value:p},p)));
  const result=el('div');
  function renderBooks() {
    const q=search.value.trim().toLowerCase();
    const filtered=data.books.filter(b=>(b.fileName+' '+(b.title||'')).toLowerCase().includes(q)&&(!select.value||b.publication===select.value));
    const groups=new Map(); filtered.forEach(b=>{if(!groups.has(b.publication))groups.set(b.publication,[]);groups.get(b.publication).push(b);});
    result.querySelectorAll('img').forEach(img=>img.coverObserver?.disconnect());result.replaceChildren();
    if(!filtered.length) result.append(empty(data.books.length?'没有匹配的刊物':'从一本刊物开始','添加 EPUB 或 PDF，已有摘要和词表会自动复用。'));
    for(const [name,books] of groups) {
      books.sort((a,b)=>a.sourceType==='podcast'&&b.sourceType==='podcast'?b.episodeNumber-a.episodeNumber:b.fileName.localeCompare(a.fileName,undefined,{numeric:true}));
      const grid=el('div',{class:'book-grid'});
      for(const book of books) {
        const isPodcast=book.sourceType==='podcast'||book.sourceType==='podcast-series';
        const cover=book.cover?cachedCover(book):isPodcast?
          el('span',{class:'podcast-cover'},el('small',{},name.replace(/ Podcast$/i,'').toUpperCase()),el('strong',{},'#'+book.episodeNumber),el('span',{},'PODCAST'),el('small',{},book.publishedAt||'TRANSCRIPT')):el('span',{class:'cover-placeholder'},name);
        const articleCount=book.articleCountKnown?`${book.articleCount} 篇文章`:'待解析文章';
        const unreadCount=book.articleCountKnown?`未读 ${book.unreadCount} 篇`:null;
        const summaryCount=book.articleCountKnown&&book.articleCount===0?'本期均为重复文章':book.summaryCount?`${book.summaryCount} 篇已有摘要`:'待生成摘要';
        grid.append(el('article',{class:'book-card'},el('a',{class:'cover-link',href:`#/book/${encodeURIComponent(book.id)}`},cover,
          el('span',{class:'format-tag'},isPodcast?'PODCAST':book.ext.toUpperCase())),
          el('h3',{},el('a',{href:`#/book/${encodeURIComponent(book.id)}`},book.title||book.fileName.replace(/\.(epub|pdf)$/i,''))),
          el('div',{class:'count'},[articleCount,unreadCount,summaryCount,book.available?null:'原文件暂不可用'].filter(Boolean).join(' · '))));
      }
      result.append(el('section',{class:'library-section'},el('div',{class:'section-title'},el('h2',{},name),el('span',{class:'count'},`${books.length} 本`)),grid));
    }
  }
  search.oninput=renderBooks;select.onchange=renderBooks;
  content.append(el('div',{class:'filters'},search,select),result);view.replaceChildren(content);renderBooks();
}

async function podcastSheet() {
  const data=await api('/podcast-sources');
  const body=el('div',{class:'form-stack'},el('p',{class:'muted'},'从官方网站导入文字 Transcript，同一播客作为一本书，每期作为一篇文章。自动使用默认 LLM 生成中文摘要，暂不生成生词表；再次导入会跳过已有内容并补缺。'));
  const sheet=showSheet('刊物来源 · Podcast',body);
  for(const source of data.sources) {
    let jobId=source.jobId;
    const status=el('p',{class:'podcast-status','aria-live':'polite'},source.status.status||'尚未导入');
    const progress=el('progress',{max:source.limit*2,value:0,'aria-label':'Podcast 导入进度'});
    const failures=el('div',{class:'podcast-failures'});
    const render=job=>{
      if(sheet!==state.sheet)return;
      const result=job.result||source.status;
      status.textContent=(job.state==='done'?result.status:'')||job.status||result.status||'准备中';
      stop.disabled=!['queued','running'].includes(job.state||result.state);
      progress.max=Math.max(1,(result.total||source.limit)*2);
      progress.value=(result.imported||0)+(result.existing||0)+(result.summaries||0)+(result.failures||[]).length;
      failures.replaceChildren(...(result.failures||[]).map(f=>el('p',{},`${f.stage==='summary'?'摘要':'Transcript'}待补缺：${f.title}`)));
    };
    const done=async job=>{if(sheet!==state.sheet)return;render(job);start.disabled=false;if(location.hash==='#/library'||!location.hash)await library(state.route);};
    const stop=button('停止导入',action(async()=>{
      if(jobId)await api(`/jobs/${encodeURIComponent(jobId)}/stop`,{method:'POST',data:{}});
      stop.disabled=true;
    }));
    stop.disabled=true;
    const start=button(`导入 / 更新最近 ${source.limit} 期`,action(async()=>{
      start.disabled=true;
      try{jobId=await runJob(`/podcast-sources/${encodeURIComponent(source.id)}/import`,{limit:source.limit},done,render);}
      catch(error){start.disabled=false;throw error;}
    }),'primary');
    const running=['queued','running'].includes(source.status.state);
    start.disabled=running;
    body.append(el('section',{class:'podcast-source'},el('h3',{},source.name),
      el('a',{href:source.url,target:'_blank',rel:'noopener noreferrer',class:'duplicate-source'},'官方网站 ↗'),
      el('p',{class:'count'},'首页只显示一个系列入口，使用官网封面。长篇摘要采用覆盖全文的分段摘录。'),
      status,progress,failures,el('div',{class:'row'},start,stop)));
    if(running&&source.jobId)watchJob(source.jobId,done,render);
    render({status:source.status.status,result:source.status});
  }
}

async function uploadSheet() {
  let quota;
  try {quota=await api('/upload-quota');} catch(error){toast(error.message);return;}
  const file=el('input',{type:'file',accept:'.epub,.pdf','aria-label':'选择刊物'});
  const megabytes=bytes=>(bytes/1024**2).toLocaleString(undefined,{maximumFractionDigits:1});
  const usage=el('p',{class:'muted upload-quota'},`单个文件最多 ${megabytes(quota.fileBytes)} MB；每日合计 ${megabytes(quota.dailyBytes)} MB，今天剩余 ${megabytes(quota.remainingBytes)} MB（按服务器日期 ${quota.day}）。`);
  const form=el('form',{class:'form-stack upload-form'},el('p',{class:'muted'},'添加 EPUB 或 PDF。摘要可在文章目录中生成。'),usage,file,
    el('button',{type:'submit',class:'primary'},'上传并加入书库'));
  form.onsubmit=action(async event=>{
    event.preventDefault();if(!file.files.length)return;
    if(file.files[0].size>quota.fileBytes)throw new Error('单个文件不得超过 50 MB');
    const submit=$('button',form);submit.disabled=true;
    try {
      quota=await api('/upload-quota');
      usage.textContent=`单个文件最多 ${megabytes(quota.fileBytes)} MB；每日合计 ${megabytes(quota.dailyBytes)} MB，今天剩余 ${megabytes(quota.remainingBytes)} MB（按服务器日期 ${quota.day}）。`;
      if(file.files[0].size>quota.remainingBytes)throw new Error('今日上传额度不足，每日累计最多 500 MB，请明天再试');
      const body=new FormData();body.append('file',file.files[0]);
      const {jobId}=await api('/books',{method:'POST',form:body,uploadSize:file.files[0].size});
      closeSheet();
      state.activeJobs.set(jobId,{status:'正在添加刊物'});displayJobBar();
      const poll=async()=>{
        try{const job=await api('/jobs/'+jobId);state.activeJobs.set(jobId,{status:job.status});displayJobBar();
          if(job.state==='done'||job.state==='error'){state.activeJobs.delete(jobId);displayJobBar();
            if(job.error)toast(job.error);else {toast('刊物已加入书库');location.hash=`#/book/${encodeURIComponent(job.result.bookId)}`;}return;}
          setTimeout(poll,700);
        }catch(error){state.activeJobs.delete(jobId);displayJobBar();toast(error.message);}
      };poll();
    }finally{submit.disabled=false;}
  });
  showSheet('添加刊物',form);
}

const labels={unread:'未读',read:'已读',uninterested:'不感兴趣'};
function issueRecentReading(articles,articleLink,isSeries) {
  const list=el('div',{class:'issue-recent-list',id:'issue-recent-list'});
  const count=el('span',{class:'count'});
  let expanded=false;
  const more=button('更多',()=>{expanded=!expanded;render();},'text-button');
  more.setAttribute('aria-controls','issue-recent-list');
  const section=el('section',{class:'issue-recent',id:'issue-recent',hidden:true,'aria-label':isSeries?'本系列继续阅读':'本刊继续阅读'},
    el('div',{class:'issue-recent-heading'},el('h2',{},'继续阅读'),count,more),list);
  function render() {
    // Keep ongoing reading accessible even if search, tags or duplicate hiding
    // exclude its directory card. Only this publication's history is considered.
    const recent=articles.filter(a=>a.history?.lastReadAt>0&&a.state==='unread'&&!a.history.atEnd&&a.history.progress<.995)
      .sort((a,b)=>b.history.lastReadAt-a.history.lastReadAt||a.index-b.index);
    section.hidden=!recent.length;
    if(recent.length<=3)expanded=false;
    count.textContent=`${isSeries?'本系列':'本刊'} · ${recent.length} 篇进行中`;
    more.hidden=recent.length<=3;more.textContent=expanded?'收起':'更多';more.setAttribute('aria-expanded',String(expanded));
    list.replaceChildren(...recent.slice(0,expanded?recent.length:3).map(a=>{
      const h=a.history,date=new Date(h.lastReadAt),title=a.titleEn||h.title;
      return el('a',{class:'issue-recent-link',href:articleLink(a),'aria-label':'继续阅读：'+title,title:[title,a.titleZh].filter(Boolean).join('\n')},
        el('strong',{class:'issue-recent-title'},title),
        el('time',{datetime:date.toISOString()},date.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})),
        el('span',{class:'issue-recent-position'},`第 ${h.paragraphIndex+1} 段 · ${Math.round(h.progress*100)}%`),
        el('span',{class:'issue-recent-arrow','aria-hidden':'true'},'→'));
    }));
  }
  return {section,render};
}
async function digestPage(bookId,token) {
  const data=await api('/books/'+encodeURIComponent(bookId));if(token!==state.route)return;
  for(const article of data.articles){
    const history=localReading.get(`/books/${encodeURIComponent(article.bookId||bookId)}/articles/${article.articleIndex??article.index}`);
    if(history&&history.lastReadAt>=(article.history?.lastReadAt||0)){article.history=history;if(history.atEnd)article.state='read';}
  }
  const isSeries=data.sourceType==='podcast-series';
  const articleLink=a=>linkFor(a.bookId||bookId,a.articleIndex??a.index);
  const recent=issueRecentReading(data.articles,articleLink,isSeries);
  const q=el('input',{class:'search',type:'search',placeholder:isSeries?'搜索期号、嘉宾、摘要、标签…':'搜索标题、摘要、标签…','aria-label':'搜索文章'});
  const topicKey='kandu-topic:'+bookId;
  let topic=sessionStorage.getItem(topicKey)||'';
  const topics=el('div',{class:'topic-filters',role:'group','aria-label':'主题标签'});
  let filter='all';const grid=el('div',{class:'article-grid'}),tabs=el('div',{class:'tabs'});
  const duplicateCount=data.articles.filter(a=>a.earlierOccurrence).length;
  const showDuplicates=el('input',{type:'checkbox',id:'show-duplicate-articles'});
  const issueCount=el('span',{class:'count',id:'issue-article-count','aria-live':'polite'});
  const visibleArticles=()=>data.articles.filter(a=>showDuplicates.checked||!a.earlierOccurrence);
  const counts=articles=>Object.fromEntries(['all',...Object.keys(labels)].map(k=>[k,k==='all'?articles.length:articles.filter(a=>a.state===k).length]));
  const cards=new Map();
  const chooseTopic=tag=>{topic=tag;sessionStorage.setItem(topicKey,tag);render();};
  const tagRow=a=>el('div',{class:'article-tags'},...(a.tags||[]).map(tag=>{
    const b=button(tag,()=>chooseTopic(tag),'topic-tag');b.setAttribute('aria-label','筛选标签：'+tag);return b;
  }));
  const editTags=a=>{
    const input=el('input',{type:'text',value:(a.tags||[]).join('，'),placeholder:'例如：AI 精选，AI 产业','aria-label':'文章标签',maxlength:240});
    const error=el('p',{class:'error',role:'alert',hidden:true});
    const submit=el('button',{type:'submit',class:'primary'},'保存标签');
    const form=el('form',{class:'form-stack'},el('p',{class:'muted'},'用逗号分隔，最多 8 个标签，每个不超过 24 个字；清空后保存可移除全部标签。'),input,error,
      el('div',{class:'row'},submit,button('取消',()=>closeSheet())));
    form.onsubmit=async event=>{
      event.preventDefault();submit.disabled=true;error.hidden=true;
      try{
        const result=await api(`/books/${encodeURIComponent(a.bookId||bookId)}/articles/${a.articleIndex??a.index}/tags`,{
          method:'POST',data:{tags:input.value.split(/[,，、;；\n]+/).map(s=>s.trim()).filter(Boolean)}});
        if(token!==state.route)return;
        a.tags=result.tags;$('.article-tags',cards.get(a.index)).replaceWith(tagRow(a));
        if(sheet===state.sheet)closeSheet();render();toast('标签已保存');
      }catch(failure){error.textContent=failure.message;error.hidden=false;}finally{submit.disabled=false;}
    };
    const sheet=showSheet('编辑文章标签',form);input.focus();
  };
  const stateSelect=(a)=>{
    const select=el('select',{'aria-label':'文章标记'},...Object.entries(labels).map(([k,v])=>el('option',{value:k},v)));
    select.value=a.state;
    select.onchange=action(async()=>{const previous=a.state;select.disabled=true;
      try{await api(`/books/${encodeURIComponent(a.bookId||bookId)}/state`,{method:'POST',data:{articleIndex:a.articleIndex??a.index,state:select.value}});a.state=select.value;
        $('.state-pill',cards.get(a.index)).textContent=labels[a.state];render();}
      catch(error){select.value=previous;throw error;}finally{select.disabled=false;}
    });return select;
  };
  for(const a of data.articles) {
    const card=el('article',{class:'article-card'},
      el('div',{class:'card-top'},el('span',{},isSeries?`EPISODE #${a.episodeNumber} · ${a.publishedAt||''}`:`ARTICLE ${String(a.index+1).padStart(2,'0')}`),el('span',{class:'state-pill'},labels[a.state])),
      el('a',{href:articleLink(a)},el('h2',{},a.titleEn),a.titleZh?el('h3',{},a.titleZh):null),
      tagRow(a),
      el('p',{},a.summaryZh||'这篇文章尚未生成摘要，可以直接打开原文阅读。'),
      a.earlierOccurrence?el('a',{class:'duplicate-source',href:linkFor(a.earlierOccurrence.bookId,a.earlierOccurrence.articleIndex)},
        '更早收录于 '+a.earlierOccurrence.fileName.replace(/\.(epub|pdf)$/i,'')+' ↗'):null,
      el('div',{class:'card-bottom'},stateSelect(a),button('标签',()=>editTags(a),'edit-tags'),el('a',{href:articleLink(a)},a.history?`继续阅读 ${Math.round(a.history.progress*100)}% →`:'打开文章 →')));
    cards.set(a.index,card);
  }
  const tabButtons=new Map();for(const [key,label] of [['all','全部'],...Object.entries(labels)]){
    const b=button(label,()=>{filter=key;render();});tabButtons.set(key,b);tabs.append(b);
  }
  function render(){
    recent.render();
    const visible=visibleArticles(),tagCounts=new Map();
    for(const a of visible)for(const tag of a.tags||[])tagCounts.set(tag,(tagCounts.get(tag)||0)+1);
    if(topic&&!tagCounts.has(topic)){topic='';sessionStorage.removeItem(topicKey);}
    const articles=visible.filter(a=>!topic||(a.tags||[]).includes(topic)),c=counts(articles);
    topics.hidden=tagCounts.size===0;topics.replaceChildren();
    for(const tag of ['',...Array.from(tagCounts.keys()).sort((a,b)=>a==='AI 精选'?-1:b==='AI 精选'?1:a.localeCompare(b,'zh-CN'))]){
      const b=button(`${tag||'全部主题'} ${tag?tagCounts.get(tag):visible.length}`,()=>chooseTopic(tag));
      b.setAttribute('aria-pressed',String(topic===tag));topics.append(b);
    }
    issueCount.textContent=(duplicateCount?`共 ${data.articles.length} 篇 · ${showDuplicates.checked?'包含 '+duplicateCount+' 篇重复文章':'已隐藏 '+duplicateCount+' 篇重复文章'}`:`${data.articles.length} ${isSeries?'期节目':'篇文章'}`)+(topic?` · ${topic} ${articles.length} 篇`:'');
    for(const [k,b]of tabButtons){b.textContent=`${k==='all'?'全部':labels[k]} ${c[k]}`;b.classList.toggle('active',k===filter);}
    const terms=q.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const matches=articles.filter(a=>(filter==='all'||a.state===filter)&&terms.every(t=>(a.titleEn+' '+a.titleZh+' '+a.summaryZh+' '+(a.episodeNumber?'#'+a.episodeNumber:'')+' '+(a.publishedAt||'')+' '+(a.guest||'')+' '+(a.tags||[]).join(' ')).toLowerCase().includes(t)));
    grid.replaceChildren(...matches.map(a=>cards.get(a.index)));
    if(!matches.length)grid.append(!articles.length&&duplicateCount&&!showDuplicates.checked?
      empty('本期文章均已在更早刊物出现','勾选“显示重复文章”可查看原目录，摘要和阅读记录仍保留。'):empty('没有匹配的文章'));
  }
  q.oninput=render;showDuplicates.onchange=render;
  view.replaceChildren(el('a',{href:'#/library',class:'back'},'← 我的书库'),
    el('div',{class:'digest-head'},heading(isSeries?'PODCAST EPISODES':'ISSUE CONTENTS',data.title||data.fileName.replace(/\.(epub|pdf)$/i,''),issueCount,[
      button('生成 / 补缺摘要',action(()=>runJob(`/books/${encodeURIComponent(bookId)}/generate-digest`,{},()=>{if(token===state.route)route();})),'primary')
    ])),...(data.sourceUrl?[el('div',{class:'podcast-provenance'},el('a',{href:data.sourceUrl,target:'_blank',rel:'noopener noreferrer'},isSeries?'播客官网 ↗':'官方 Transcript ↗'),
      data.excerpted?el('span',{class:'count'},'摘要基于覆盖全文的分段摘录；正文为完整 Transcript。'):null)]:[]),
    recent.section,el('div',{class:'filters'},q,duplicateCount?el('label',{class:'duplicate-toggle'},showDuplicates,`显示重复文章（${duplicateCount}）`):null),topics,tabs,grid);
  render();
}

function currentPosition(reader=state.reader) {
  if(!reader||!reader.ready)return null;
  const paragraphs=reader.paragraphNodes;
  if(!paragraphs.length)return null;
  const top=$('.reader-toolbar')?.getBoundingClientRect().bottom||80;
  let chosen=paragraphs.find(p=>p.getBoundingClientRect().bottom>top+8)||paragraphs.at(-1);
  const index=Number(chosen.dataset.index),paragraph=$('p',chosen),rect=paragraph.getBoundingClientRect();
  const fraction=Math.max(0,Math.min(1,(top-rect.top)/Math.max(1,rect.height)));
  let offset=Math.floor(reader.article.paragraphs[index].length*fraction);
  let atEnd=window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-65;
  if(atEnd){chosen=paragraphs.at(-1);offset=reader.article.paragraphs[Number(chosen.dataset.index)].length;}
  return {paragraphIndex:Number(chosen.dataset.index),paragraphOffset:offset};
}
function updateReaderProgress(reader=state.reader) {
  if(state.reader!==reader)return;
  const position=currentPosition(reader);if(!position)return;
  const value=Math.max(0,Math.min(1,((reader.paragraphOffsets[position.paragraphIndex]||0)+position.paragraphOffset)/Math.max(1,reader.totalCharacters)));
  $('#reader-progress-bar').value=value;
  $('#reader-percentage').textContent=`阅读进度 ${value===1?100:Math.min(99,Math.floor(value*100))}%`;
  $('#reader-paragraph').textContent=`第 ${position.paragraphIndex+1} / ${reader.article.paragraphs.length} 段`;
}
function progressStatus(reader,text) {
  if(state.reader===reader&&$('#reader-save-state'))$('#reader-save-state').textContent=text;
}
function saveProgress(keepalive=false,reader=state.reader) {
  const position=currentPosition(reader);if(!position)return Promise.resolve();
  const key=JSON.stringify(position);if(reader.lastPosition===key)return Promise.resolve();
  reader.lastPosition=key;
  const path=baseFor(reader),observedAt=ReadingSync.now();
  const progress=((reader.paragraphOffsets[position.paragraphIndex]||0)+position.paragraphOffset)/Math.max(1,reader.totalCharacters);
  const record={...position,title:reader.article.title,titleZh:reader.titleZh,bookTitle:reader.bookTitle,lastReadAt:observedAt,
    paragraphCount:reader.article.paragraphs.length,wordCount:reader.wordCount,contentVersion:reader.contentVersion,progress,atStart:progress===0,atEnd:progress>=.995};
  reader.history=record;localReading.set(path,record);
  updateCachedHistory(path,record);
  ReadingSync.add(path+'/progress',{...position,observedAt,contentVersion:reader.contentVersion});
  progressStatus(reader,'本机已保存 · 待同步');
  return ReadingSync.drain().catch(()=>{progressStatus(reader,'本机已保存 · 待同步');});
}
const localReading=new Map();
async function updateCachedHistory(path,history){
  const record=await ReadingCache.get(path);
  if(record)await ReadingCache.put(path,{...record.value,history},{...record,checked:0});
}
window.addEventListener('reading-synced',event=>{
  const {path,data,result}=event.detail,reader=state.reader;
  if(path.endsWith('/progress')){
    const base=path.slice(0,-'/progress'.length),local=localReading.get(base);
    if(!local||result.record.lastReadAt>=local.lastReadAt){const history={...local,...result.record};localReading.set(base,history);updateCachedHistory(base,history);}
    if(reader&&baseFor(reader)===base&&reader.lastPosition===JSON.stringify({paragraphIndex:data.paragraphIndex,paragraphOffset:data.paragraphOffset})){
      progressStatus(reader,result.accepted===false?'服务器有较新的阅读位置':'已同步');
    }
    ReadingCache.stale(key=>cacheable(key)&&!key.endsWith('/vocab'));
  }else if(reader&&path===baseFor(reader)+'/reading-time'&&reader.clock?.id===data.sessionId){
    reader.clock.serverMs=result.totalActiveMs;reader.clock.syncedElapsed=Math.max(reader.clock.syncedElapsed,data.elapsedMs);
    reader.clock.lastSaved=JSON.stringify([data.elapsedMs,{paragraphIndex:data.paragraphIndex,paragraphOffset:data.paragraphOffset}]);
    reader.clock.syncFailed=false;readingClockTick(reader);
  }
});

function readingDuration(ms) {
  const seconds=Math.floor(ms/1000),minutes=Math.floor(seconds/60);
  return minutes>=60?`${Math.floor(minutes/60)} 小时 ${minutes%60} 分`:minutes?`${minutes} 分 ${seconds%60} 秒`:`${seconds} 秒`;
}
function pauseVocabularyGeneration(path) {
  const token=Symbol(),articlePath=path.slice(0,-'/generate-vocab'.length);
  readingClockTick();state.vocabWaits.set(token,articlePath);readingClockTick();
  flushReadingClock();
  return ()=>{
    if(!state.vocabWaits.has(token))return;
    readingClockTick();state.vocabWaits.delete(token);readingClockTick();
  };
}
function readingClockReason(reader,now=performance.now()) {
  if(reader.clock.completedPaused)return '已读停计时';
  if(reader.markingRead)return '正在标记已读';
  if(reader.clock.manualPaused)return '已暂停';
  if(document.hidden)return '后台暂停';
  if(!document.hasFocus())return '失焦暂停';
  if([...state.vocabWaits.values()].includes(baseFor(reader)))return '生词生成暂停';
  if($('#sheet').open)return '弹窗暂停';
  if(reader.hoverVisible)return '词义提示暂停';
  if(now-reader.clock.lastInteraction>90_000)return '无操作暂停';
  return '计时中';
}
function setReaderState(reader,value){
  readingClockTick(reader);reader.state=value;
  if(reader.clock)reader.clock.completedPaused=value==='read';
  readingClockTick(reader);updateReadingClockControl(reader);
  if(state.reader!==reader)return;
  const read=value==='read';
  const mark=$('#reader-mark-read');if(mark)mark.textContent=read?'已读 ✓':'标记已读';
  if(read)flushReadingClock(reader);
}
function updateReadingClockControl(reader){
  if(state.reader!==reader)return;
  const timer=$('#reading-time-toggle');if(!timer)return;
  const paused=!!(reader.clock?.completedPaused||reader.clock?.manualPaused);
  timer.textContent=paused?'继续计时':'暂停计时';
  timer.setAttribute('aria-pressed',String(paused));
  timer.title=reader.clock?.completedPaused?'文章已读，点击可继续本次重读计时，保留已读标记。':'暂停期间不累计阅读时间；点击继续计时后恢复。';
}
function readingClockTick(reader=state.reader) {
  if(!reader?.clock||reader.clock.stopped)return;
  const clock=reader.clock,now=performance.now(),delta=now-clock.lastSample;
  // Long timer gaps indicate a suspended tab/device, not continuous reading.
  if(delta>=0&&delta<=2500&&readingClockReason(reader,clock.lastSample)==='计时中') {
    const until=Math.min(now,clock.lastInteraction+90_000);
    clock.elapsedMs+=Math.max(0,until-clock.lastSample);
  }
  clock.lastSample=now;
  const status=$('#reader-reading-time');
  if(state.reader===reader&&status) {
    const total=clock.serverMs+Math.max(0,clock.elapsedMs-clock.syncedElapsed);
    const message=`${readingClockReason(reader,now)} · ${readingDuration(total)}${clock.syncFailed?' · 待同步':''}`;
    if(status.textContent!==message)status.textContent=message;
  }
}
function flushReadingClock(reader=state.reader,keepalive=false,initialPosition=null) {
  if(!reader?.clock)return Promise.resolve();
  const clock=reader.clock;
  const position=initialPosition||currentPosition(reader);if(!position)return Promise.resolve();
  const elapsedMs=Math.floor(clock.elapsedMs),signature=JSON.stringify([elapsedMs,position]);
  if(signature===clock.lastQueued)return clock.sending||Promise.resolve();
  clock.lastQueued=signature;
  const data={...position,sessionId:clock.id,sequence:clock.sequence++,elapsedMs,sampledAt:ReadingSync.now(),contentVersion:reader.contentVersion};
  ReadingSync.add(baseFor(reader)+'/reading-time',data);
  const pending=ReadingSync.drain().catch(()=>{clock.syncFailed=true;}).finally(()=>{if(clock.sending===pending)clock.sending=null;});
  clock.sending=pending;
  return clock.sending;
}
function startReadingClock(reader,initialPosition) {
  const now=performance.now();
  reader.clock={id:readingId(),
    sequence:0,elapsedMs:0,syncedElapsed:0,serverMs:reader.readingTiming?.activeMs||0,
    lastSample:now,lastInteraction:now,manualPaused:false,completedPaused:reader.state==='read',stopped:false};
  const clock=reader.clock;
  clock.interaction=event=>{if(event.isTrusted){readingClockTick(reader);clock.lastInteraction=performance.now();}};
  for(const name of ['pointerdown','pointermove','keydown','wheel','touchstart'])window.addEventListener(name,clock.interaction,{passive:true});
  clock.focus=()=>{readingClockTick(reader);clock.lastInteraction=performance.now();};
  window.addEventListener('focus',clock.focus);
  clock.blur=()=>{readingClockTick(reader);flushReadingClock(reader,true);};
  window.addEventListener('blur',clock.blur);
  clock.interval=setInterval(()=>{readingClockTick(reader);if(++clock.ticks%15===0)flushReadingClock(reader);},1000);
  clock.ticks=0;
  flushReadingClock(reader,false,initialPosition);readingClockTick(reader);
  setReaderState(reader,reader.state);
}
function stopReadingClock(reader,save=true) {
  if(!reader?.clock)return Promise.resolve();
  readingClockTick(reader);
  const clock=reader.clock;clock.stopped=true;clearInterval(clock.interval);
  for(const name of ['pointerdown','pointermove','keydown','wheel','touchstart'])window.removeEventListener(name,clock.interaction);
  window.removeEventListener('focus',clock.focus);
  window.removeEventListener('blur',clock.blur);
  return save?flushReadingClock(reader,true):Promise.resolve();
}
function selectedQuote() {
  const selection=window.getSelection();if(!selection||selection.isCollapsed)return null;
  const node=selection.anchorNode?.parentElement?.closest('.paragraph');
  const end=selection.focusNode?.parentElement?.closest('.paragraph');
  if(!node||node!==end)return null;
  const quote=selection.toString().trim();if(!quote||quote.length>5000)return null;
  const range=selection.getRangeAt(0),text=$('p',node);
  if(!text.contains(range.startContainer)||!text.contains(range.endContainer))return null;
  const prefix=range.cloneRange();prefix.selectNodeContents(text);prefix.setEnd(range.startContainer,range.startOffset);
  const leading=selection.toString().match(/^\s*/)[0];
  return {quote,paragraph:Number(node.dataset.index),start:[...prefix.toString()].length+[...leading].length};
}
function clearReaderSelection(reader,clearNative=false) {
  reader.touchSelection=null;reader.selectionRange=null;state.selection=null;
  window.CSS?.highlights?.delete('kandu-touch-selection');
  const bar=$('#selection-tools');if(bar)bar.hidden=true;
  if(clearNative)getSelection()?.removeAllRanges();
}
function showReaderSelection(reader,chosen,range,touch=false) {
  reader.hoverHide?.();state.selection=chosen;reader.selectionRange=range;
  reader.touchSelection=touch?chosen:null;
  window.CSS?.highlights?.delete('kandu-touch-selection');
  if(touch&&supportsVocabHighlights())CSS.highlights.set('kandu-touch-selection',new Highlight(range));
  const bar=$('#selection-tools');bar.hidden=false;bar.classList.toggle('touch-selection',touch);
  $('span',bar).textContent=chosen.quote;
  $('#selection-expand').hidden=!touch;
  const whole=touch&&chosen.quote===sentenceSelection(reader,chosen).quote;
  $('#selection-expand').disabled=whole;$('#selection-expand').textContent=whole?'已选整句':'选整句';
  positionSelectionBar(reader);
}
function sentenceSelection(reader,chosen) {
  const text=reader.article.paragraphs[chosen.paragraph];
  const at=Number.isInteger(chosen.start)?[...text].slice(0,chosen.start).join('').length:text.indexOf(chosen.quote);
  const segments=typeof Intl.Segmenter==='function'?[...new Intl.Segmenter('en',{granularity:'sentence'}).segment(text)]
    :(text.match(/[^.!?]+[.!?]+["'’”]?|[^.!?]+$/g)||[text]).map((segment,i,all)=>({segment,index:all.slice(0,i).join('').length}));
  const found=segments.find(s=>at>=s.index&&at<s.index+s.segment.length);
  const raw=found?.segment||text,start=(found?.index||0)+(raw.length-raw.trimStart().length),quote=raw.trim();
  return {quote,paragraph:chosen.paragraph,start:[...text.slice(0,start)].length};
}
function installTouchReading(reader,body,toggle) {
  const controller=new AbortController(),options={signal:controller.signal};
  const media=matchMedia('(pointer:coarse)');let touch=null;
  const cancelPress=()=>{touch=null;body.classList.remove('point-press');};
  reader.pointMode=localStorage.getItem('kandu-touch-mode')!=='select';
  const update=()=>{
    reader.pointActive=media.matches&&reader.pointMode;
    body.classList.toggle('point-reading',reader.pointActive);
    toggle.textContent=reader.pointActive?'切换为系统选文':'切换为点词';
    toggle.setAttribute('aria-pressed',String(reader.pointActive));
    const help=$('#touch-reading-help');if(help)help.textContent=reader.pointActive?
      '轻点单词，可查词、选整句或提问。「更多」中可切换系统选文。':'长按文字并拖动选区，可选短语、复制；刊读操作栏显示在屏幕底部。';
  };
  toggle.onclick=()=>{
    cancelPress();
    clearReaderSelection(reader,true);reader.pointMode=!reader.pointMode;
    localStorage.setItem('kandu-touch-mode',reader.pointMode?'point':'select');update();
    $('.reader-toolbar')?.classList.remove('options-open');$('#reader-more-toggle')?.setAttribute('aria-expanded','false');
    toast(reader.pointActive?'点词模式：轻点单词，再选查词、整句或提问。':'系统选文：长按并拖动选区，可选短语和复制文字。');
  };
  media.addEventListener('change',update,options);update();
  body.addEventListener('contextmenu',event=>{
    if(reader.pointActive&&event.target.closest('.paragraph p'))event.preventDefault();
  },options);
  body.addEventListener('selectstart',event=>{
    const target=event.target instanceof Element?event.target:event.target.parentElement;
    if(reader.pointActive&&target?.closest('.paragraph p'))event.preventDefault();
  },options);
  body.addEventListener('pointerdown',event=>{
    cancelPress();
    if(!reader.pointActive||event.pointerType==='mouse'||!event.isPrimary||event.button!==0)return;
    const p=event.target.closest('.paragraph p');if(!p)return;
    touch={id:event.pointerId,x:event.clientX,y:event.clientY,time:performance.now(),scroll:scrollY,p};
    // WebKit skips Custom Highlight painting on user-select:none text. Suppress
    // native selection only during the press, so existing vocabulary and the
    // tapped range remain painted after release. Pan/pinch keep browser defaults.
    body.classList.add('point-press');
  },{...options,passive:true});
  body.addEventListener('pointermove',event=>{
    if(touch&&Math.hypot(event.clientX-touch.x,event.clientY-touch.y)>10)cancelPress();
  },{...options,passive:true});
  body.addEventListener('pointercancel',cancelPress,options);
  body.addEventListener('pointerup',event=>{
    const start=touch;cancelPress();
    if(!start||event.pointerId!==start.id||performance.now()-start.time>550||Math.abs(scrollY-start.scroll)>3||$('#sheet').open)return;
    const caret=document.caretPositionFromPoint?.(event.clientX,event.clientY);
    const fallback=caret?null:document.caretRangeFromPoint?.(event.clientX,event.clientY);
    const node=caret?.offsetNode||fallback?.startContainer,offset=caret?.offset??fallback?.startOffset;
    if(!node||!start.p.contains(node))return;
    const prefix=document.createRange();prefix.selectNodeContents(start.p);prefix.setEnd(node,offset);
    const text=start.p.textContent,at=prefix.toString().length;
    const word=vocabHoverTokens(text).find(t=>at>=t.start&&at<=t.end);
    const range=word&&vocabHoverRange(start.p,word.start,word.end);
    if(!range||![...range.getClientRects()].some(r=>event.clientX>=r.left-1&&event.clientX<=r.right+1&&event.clientY>=r.top&&event.clientY<=r.bottom)){
      clearReaderSelection(reader);return;
    }
    showReaderSelection(reader,{quote:text.slice(word.start,word.end),paragraph:Number(start.p.closest('.paragraph').dataset.index),
      start:[...text.slice(0,word.start)].length},range,true);
  },options);
  // A gesture that becomes a pan/pinch must never turn into a lookup.
  document.addEventListener('pointerdown',event=>{
    if(!event.isPrimary)cancelPress();
    if(reader.touchSelection&&!event.target.closest('#selection-tools,.paragraph p'))clearReaderSelection(reader);
    if(!event.target.closest('.reader-toolbar')){
      $('.reader-toolbar')?.classList.remove('options-open');$('#reader-more-toggle')?.setAttribute('aria-expanded','false');
    }
  },{...options,passive:true});
  document.addEventListener('keydown',event=>{
    if(event.key!=='Escape'||$('#sheet').open)return;
    clearReaderSelection(reader);$('.reader-toolbar')?.classList.remove('options-open');$('#reader-more-toggle')?.setAttribute('aria-expanded','false');
  },options);
  window.addEventListener('blur',cancelPress,options);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)cancelPress();},options);
  reader.touchCleanup=()=>{cancelPress();controller.abort();clearReaderSelection(reader);};
}
function selectionBar(reader) {
  reader.hoverHide?.();
  if($('#sheet').open)return;
  const selection=selectedQuote(),bar=$('#selection-tools');
  if(!selection&&reader.touchSelection){positionSelectionBar(reader);return;}
  if(!selection){bar.hidden=true;state.selection=null;reader.selectionRange=null;return;}
  showReaderSelection(reader,selection,window.getSelection().getRangeAt(0).cloneRange());
}
// Index existing entries once when the vocabulary changes. Prose stays as text,
// including search marks: no per-word DOM nodes, network calls or model requests.
function vocabHoverTokens(text) {
  return [...text.matchAll(/[A-Za-z]+(?:['’‘][A-Za-z]+)?/g)].map(m=>({
    word:m[0].toLowerCase().replace(/[’‘]/g,"'"),start:m.index,end:m.index+m[0].length
  }));
}
function vocabHoverForms(word) {
  const forms=new Set([word]);
  if(word.endsWith("'s"))forms.add(word.slice(0,-2));
  if(word.endsWith('s')&&!word.endsWith('ss'))forms.add(word.slice(0,-1));
  if(word.endsWith('es'))forms.add(word.slice(0,-2));
  if(word.endsWith('ies'))forms.add(word.slice(0,-3)+'y');
  if(word.endsWith('ed')) {
    forms.add(word.slice(0,-1));forms.add(word.slice(0,-2));
    if(word.length>4&&word.at(-3)===word.at(-4))forms.add(word.slice(0,-3));
    if(word.endsWith('ied'))forms.add(word.slice(0,-3)+'y');
  }
  if(word.endsWith('ing')) {
    forms.add(word.slice(0,-3));forms.add(word.slice(0,-3)+'e');
    if(word.length>5&&word.at(-4)===word.at(-5))forms.add(word.slice(0,-4));
    if(word.endsWith('ying'))forms.add(word.slice(0,-4)+'ie');
  }
  return forms;
}
function indexVocabHover(reader) {
  reader.hoverHide?.();
  const vocab=reader.vocab.vocab,byFirst=new Map(),groups=new Map();
  for(const section of [{index:-1,entries:vocab.titleEntries},...vocab.paragraphs]) {
    for(const entry of section.entries) {
      const words=vocabHoverTokens(entry.word||'').map(t=>t.word);if(!words.length)continue;
      const key=words.join(' ');
      if(!groups.has(key)) {
        const item={words,entries:new Map(),fallback:entry};groups.set(key,item);
        if(!byFirst.has(words[0]))byFirst.set(words[0],[]);
        byFirst.get(words[0]).push(item);
      }
      groups.get(key).entries.set(section.index,entry);
    }
  }
  reader.hoverWords=new Map();
  for(const [paragraph,text] of [[-1,reader.article.title],...reader.article.paragraphs.map((p,i)=>[i,p])]) {
    const tokens=vocabHoverTokens(text),forms=tokens.map(t=>vocabHoverForms(t.word));
    for(let i=0;i<tokens.length;i++) {
      for(const form of forms[i])for(const item of byFirst.get(form)||[]) {
        const {words}=item,local=item.entries.has(paragraph),entry=item.entries.get(paragraph)||item.fallback;
        if(i+words.length>tokens.length||!words.every((w,j)=>forms[i+j].has(w)))continue;
        // Match phrases across spaces/hyphens, never across sentence punctuation.
        if(words.some((_,j)=>j&&!/^[\s\-–—]+$/.test(text.slice(tokens[i+j-1].end,tokens[i+j].start))))continue;
        const score=(local?1000:0)+words.length*10+(form===tokens[i].word?1:0);
        for(let j=0;j<words.length;j++)if(!tokens[i+j].entry||score>tokens[i+j].score) {
          tokens[i+j].entry=entry;tokens[i+j].score=score;
          tokens[i+j].highlightStart=tokens[i].start;tokens[i+j].highlightEnd=tokens[i+words.length-1].end;
        }
      }
    }
    reader.hoverWords.set(paragraph,tokens);
  }
}
function supportsVocabHighlights() { return Boolean(window.CSS?.highlights&&typeof Highlight==='function'); }
function updateVocabHighlights(reader) {
  if(!supportsVocabHighlights())return;
  const highlight=new Highlight();
  if(reader.vocabHighlightEnabled)for(let paragraph=0;paragraph<reader.paragraphNodes.length;paragraph++) {
    const p=$('p',reader.paragraphNodes[paragraph]),spans=[];
    // Merge repeated phrase spans so the spaces in "paper over" share one tint.
    const matches=(reader.hoverWords?.get(paragraph)||[]).filter(t=>t.entry)
      .map(t=>({start:t.highlightStart,end:t.highlightEnd})).sort((a,b)=>a.start-b.start);
    for(const match of matches) {
      const previous=spans.at(-1);
      if(previous&&match.start<=previous.end)previous.end=Math.max(previous.end,match.end);
      else spans.push(match);
    }
    if(!spans.length)continue;
    const walker=document.createTreeWalker(p,NodeFilter.SHOW_TEXT);let node,offset=0,first=0;
    while((node=walker.nextNode())) {
      const end=offset+node.length;
      while(first<spans.length&&spans[first].end<=offset)first++;
      // Search results keep their distinct gold background, even within a word.
      if(!node.parentElement.closest('mark'))for(let i=first;i<spans.length&&spans[i].start<end;i++) {
        const start=Math.max(offset,spans[i].start),stop=Math.min(end,spans[i].end);
        if(stop<=start)continue;
        const range=document.createRange();range.setStart(node,start-offset);range.setEnd(node,stop-offset);highlight.add(range);
      }
      offset=end;
    }
  }
  if(highlight.size)CSS.highlights.set('kandu-vocabulary',highlight);
  else CSS.highlights.delete('kandu-vocabulary');
}
function vocabHoverRange(p,start,end) {
  const range=document.createRange(),walker=document.createTreeWalker(p,NodeFilter.SHOW_TEXT);
  let node,offset=0,started=false;
  while((node=walker.nextNode())) {
    const next=offset+node.length;
    if(!started&&start<next){range.setStart(node,start-offset);started=true;}
    if(started&&end<=next){range.setEnd(node,end-offset);return range;}
    offset=next;
  }
  return null;
}
function installVocabHover(reader,article) {
  const tip=el('aside',{id:'vocab-hover',class:'vocab-hover',role:'tooltip',hidden:true});
  article.append(tip);
  const controller=new AbortController(),options={signal:controller.signal,passive:true};
  let timer=null,hover=null;
  reader.hoverHide=()=>{
    clearTimeout(timer);timer=null;hover=null;
    if(!reader.hoverVisible)return;
    readingClockTick(reader);reader.hoverVisible=false;tip.hidden=true;readingClockTick(reader);
  };
  reader.hoverCleanup=()=>{reader.hoverHide();controller.abort();tip.remove();window.CSS?.highlights?.delete('kandu-vocabulary');};
  const blocked=()=>state.reader!==reader||$('#sheet').open||document.hidden||!document.hasFocus()||!getSelection()?.isCollapsed;
  article.addEventListener('pointermove',event=>{
    if(event.pointerType!=='mouse'||event.buttons||blocked()){reader.hoverHide();return;}
    const p=event.target.closest('.paragraph p, .reader-head h1');
    if(!p){reader.hoverHide();return;}
    const caret=document.caretPositionFromPoint?.(event.clientX,event.clientY);
    const fallback=caret?null:document.caretRangeFromPoint?.(event.clientX,event.clientY);
    const node=caret?.offsetNode||fallback?.startContainer,offset=caret?.offset??fallback?.startOffset;
    if(!node||!p.contains(node)){reader.hoverHide();return;}
    const prefix=document.createRange();prefix.selectNodeContents(p);prefix.setEnd(node,offset);
    const at=prefix.toString().length,paragraph=Number(p.closest('.paragraph')?.dataset.index??-1);
    const token=reader.hoverWords?.get(paragraph)?.find(t=>t.entry&&at>=t.start&&at<=t.end);
    const range=token&&vocabHoverRange(p,token.start,token.end);
    const rect=range&&[...range.getClientRects()].find(r=>event.clientX>=r.left&&event.clientX<=r.right&&event.clientY>=r.top&&event.clientY<=r.bottom);
    if(!rect){reader.hoverHide();return;}
    if(hover?.token===token&&hover.p===p)return;
    reader.hoverHide();hover={token,p};
    timer=setTimeout(()=>{
      if(blocked()||!p.isConnected){reader.hoverHide();return;}
      const entry=token.entry;
      tip.replaceChildren(el('div',{class:'hover-heading'},el('strong',{},entry.word),el('span',{},[
        entry.phonetic?'/'+entry.phonetic.replaceAll('/','')+'/':'',entry.pos||''].filter(Boolean).join('  '))),
        el('p',{class:'hover-sense'},entry.sense||'暂无释义'),
        entry.inText?el('p',{class:'hover-context'},entry.inText):null,
        entry.root?el('p',{class:'hover-detail'},'词根 / 词源 · '+entry.root):null,
        entry.note?el('p',{class:'hover-detail'},'笔记 · '+entry.note):null,
        el('small',{},'本文生词表 · 完整内容可点「本段生词」'));
      const {left,top,width,height}=readingViewport(),margin=10;
      tip.style.maxWidth=Math.max(0,width-2*margin)+'px';tip.style.maxHeight=Math.max(0,height-2*margin)+'px';
      tip.hidden=false;
      const size=tip.getBoundingClientRect();let y=rect.bottom+8;
      if(y+size.height>top+height-margin)y=rect.top-size.height-8;
      tip.style.left=Math.max(left+margin,Math.min(rect.left,left+width-size.width-margin))+'px';
      tip.style.top=Math.max(top+margin,Math.min(y,top+height-size.height-margin))+'px';
      readingClockTick(reader);reader.hoverVisible=true;readingClockTick(reader);
    },1000);
  },options);
  article.addEventListener('pointerleave',reader.hoverHide,options);
  for(const event of ['pointerdown','keydown','wheel','scroll','blur','resize','pagehide'])
    window.addEventListener(event,reader.hoverHide,{...options,capture:true});
  document.addEventListener('visibilitychange',reader.hoverHide,options);
  window.visualViewport?.addEventListener('resize',reader.hoverHide,options);
  window.visualViewport?.addEventListener('scroll',reader.hoverHide,options);
}
function positionSelectionBar(reader) {
  const bar=$('#selection-tools');if(!bar||bar.hidden||!reader.selectionRange||$('#sheet').open)return;
  const {left,top,width,height}=readingViewport(),margin=10;
  const toolbarBottom=$('.reader-toolbar')?.getBoundingClientRect().bottom||top;
  const rects=[...reader.selectionRange.getClientRects()].filter(r=>r.width&&r.height&&r.bottom>Math.max(top,toolbarBottom)&&r.top<top+height);
  if(!rects.length){if(reader.touchSelection)clearReaderSelection(reader);else bar.hidden=true;return;}
  const selection=window.getSelection(),range=reader.selectionRange;
  const forward=selection?.anchorNode===range.startContainer&&selection?.anchorOffset===range.startOffset;
  const anchor=forward?rects.at(-1):rects[0];
  bar.style.maxWidth=Math.max(0,width-margin*2)+'px';
  // Native selection handles and the OS copy/translate menu stay near the text.
  // Keep our actions at the opposite edge while users adjust that selection.
  if(matchMedia('(pointer:coarse)').matches&&!reader.touchSelection){
    bar.classList.add('native-touch-selection');
    bar.style.left=(left+margin)+'px';bar.style.top=(top+height-bar.getBoundingClientRect().height-margin)+'px';return;
  }
  bar.classList.remove('native-touch-selection');
  const size=bar.getBoundingClientRect();
  let y=anchor.bottom+9;
  if(y+size.height>top+height-margin)y=anchor.top-size.height-9;
  const x=Math.max(left+margin,Math.min(anchor.left+anchor.width/2-size.width/2,left+width-size.width-margin));
  bar.style.left=x+'px';bar.style.top=Math.max(top+margin,Math.min(y,top+height-size.height-margin))+'px';
}
async function readerPage(bookId,index,token,startParagraph=null) {
  const data=await api(`/books/${encodeURIComponent(bookId)}/articles/${index}`);if(token!==state.route)return;
  const path=`/books/${encodeURIComponent(bookId)}/articles/${index}`;
  const queued=ReadingSync.entries().filter(item=>item.path===path+'/progress').at(-1);
  const local=localReading.get(path);
  if(local&&(!local.contentVersion||local.contentVersion===data.contentVersion)&&local.lastReadAt>=(data.history?.lastReadAt||0))data.history=local;
  if(queued&&(!queued.data.contentVersion||queued.data.contentVersion===data.contentVersion)&&queued.data.observedAt>=(data.history?.lastReadAt||0))data.history={...queued.data,lastReadAt:queued.data.observedAt,atStart:queued.data.paragraphIndex===0&&queued.data.paragraphOffset===0};
  const reader={...data,bookId,index,ready:false,vocab:null,fontSize:Number(localStorage.getItem('kandu-font')||21),
    vocabHighlightEnabled:localStorage.getItem('kandu-vocab-highlight')!=='off'};state.reader=reader;
  reader.paragraphOffsets=[];reader.totalCharacters=0;
  for(const paragraph of data.article.paragraphs){reader.paragraphOffsets.push(reader.totalCharacters);reader.totalCharacters+=paragraph.length;}
  const article=el('article',{class:'reader'});
  const progress=el('div',{class:'reader-position'},el('div',{class:'reader-position-labels'},
    el('strong',{id:'reader-percentage'},'阅读进度 0%'),el('span',{id:'reader-paragraph'},`第 1 / ${data.article.paragraphs.length} 段`),
    el('span',{id:'reader-word-count'},`共 ${data.wordCount.toLocaleString()} 词`),
    el('span',{id:'reader-reading-time',title:'前台阅读自动计时；生词生成、词义提示、失焦、弹窗或 90 秒无操作时暂停。阅读速度根据计时期间前进的词数估算。'},'准备计时'),
    el('span',{class:'reader-save-state',id:'reader-save-state'},'自动同步')),
    el('progress',{id:'reader-progress-bar',max:1,value:0,'aria-label':'文章阅读进度'}));
  const vocabButton=button('生成生词表',action(()=>openVocab(null)),'primary');vocabButton.id='vocab-button';
  const find=el('input',{type:'search',placeholder:'在本文中查找…','aria-label':'查找原文'});
  const findRow=el('div',{class:'find-row',hidden:true},find,el('span',{class:'count',id:'find-count'}),button('×',()=>{find.value='';findArticle(reader,'');findRow.hidden=true;}));
  const pauseTimer=button('暂停计时',()=>{
    if(reader.markingRead)return;
    readingClockTick(reader);
    if(reader.clock.completedPaused){reader.clock.completedPaused=false;reader.clock.manualPaused=false;}
    else reader.clock.manualPaused=!reader.clock.manualPaused;
    reader.clock.lastInteraction=performance.now();updateReadingClockControl(reader);
    readingClockTick(reader);flushReadingClock(reader);
  });pauseTimer.id='reading-time-toggle';pauseTimer.setAttribute('aria-pressed','false');
  pauseTimer.title='暂停期间不累计阅读时间；点击继续计时后恢复。';
  progress.insertBefore(pauseTimer,progress.lastElementChild);
  const highlightToggle=button('生词标记',()=>{
    reader.vocabHighlightEnabled=!reader.vocabHighlightEnabled;
    localStorage.setItem('kandu-vocab-highlight',reader.vocabHighlightEnabled?'on':'off');
    highlightToggle.setAttribute('aria-pressed',String(reader.vocabHighlightEnabled));updateVocabHighlights(reader);
  },'vocab-highlight-toggle');
  highlightToggle.id='vocab-highlight-toggle';highlightToggle.hidden=!supportsVocabHighlights();
  highlightToggle.setAttribute('aria-pressed',String(reader.vocabHighlightEnabled));
  highlightToggle.title='用浅绿色背景标出本文词表中的生词和短语，可随时关闭；不额外生成词表。';
  const pointToggle=button('切换为系统选文',()=>{},'touch-mode-toggle');pointToggle.id='touch-mode-toggle';
  const more=button('更多',()=>{
    const open=tools.classList.toggle('options-open');more.setAttribute('aria-expanded',String(open));
  },'reader-more-toggle');more.id='reader-more-toggle';more.setAttribute('aria-expanded','false');more.setAttribute('aria-controls','reader-options');
  const tools=el('div',{class:'reader-toolbar'},el('div',{class:'reader-primary'},
    el('a',{href:`#/book/${encodeURIComponent(data.parentBookId||bookId)}`},'← 目录'),vocabButton,
    button('与 AI 讨论',action(()=>discussionSheet(reader))),more),
    el('div',{class:'reader-options',id:'reader-options'},
    el('button',{type:'button',id:'reader-mark-read',onclick:action(async event=>{
      const target=event.target.closest('button');target.disabled=true;
      readingClockTick(reader);reader.markingRead=true;readingClockTick(reader);flushReadingClock(reader);
      try{await api(`/books/${encodeURIComponent(bookId)}/state`,{method:'POST',data:{articleIndex:index,state:'read'}});
        setReaderState(reader,'read');
        const cached=await ReadingCache.get(path);
        if(cached)await ReadingCache.put(path,{...cached.value,state:'read'},{...cached,checked:0});
        toast('已计入全局已读统计');}finally{reader.markingRead=false;readingClockTick(reader);target.disabled=false;}
    })},data.state==='read'?'已读 ✓':'标记已读'),
    button('查找',()=>{findRow.hidden=!findRow.hidden;if(!findRow.hidden){tools.classList.remove('options-open');more.setAttribute('aria-expanded','false');find.focus();}}),highlightToggle,pointToggle,
    el('span',{class:'grow'}),button('A−',()=>resizeReader(reader,-1)),button('A+',()=>resizeReader(reader,1)),
    el('a',{href:`/api/books/${encodeURIComponent(bookId)}/file`,target:'_blank',rel:'noopener'},data.ext==='pdf'?'原版 PDF ↗':'下载 EPUB ↗')),
    progress);
  tools.addEventListener('click',event=>{
    const target=event.target.closest('#reader-options button,#reader-options a');
    if(target&&!['A−','A+'].includes(target.textContent)){
      tools.classList.remove('options-open');more.setAttribute('aria-expanded','false');
    }
  });
  const selection=el('div',{class:'selection-tools',id:'selection-tools',hidden:true,role:'toolbar','aria-label':'选中文字操作',
    onpointerdown:event=>event.preventDefault()},el('span',{}),
    el('button',{type:'button',id:'selection-expand',hidden:true,onclick:()=>{
      const chosen=sentenceSelection(reader,state.selection),text=reader.article.paragraphs[chosen.paragraph];
      const start=[...text].slice(0,chosen.start).join('').length;
      showReaderSelection(reader,chosen,vocabHoverRange($('#para-'+chosen.paragraph+' p'),start,start+chosen.quote.length),true);
    }},'选整句'),
    button('查词并收藏',action(()=>lookupSelection(reader))),button('句子讲解',action(()=>analyzeSelection(reader))),
    button('引用提问',action(()=>discussionSheet(reader,state.selection))),
    el('button',{type:'button','aria-label':'关闭选词操作',onclick:()=>clearReaderSelection(reader,true)},'×'));
  const body=el('div',{class:'reader-body',id:'reader-body'});
  body.style.setProperty('--reading-size',reader.fontSize+'px');
  data.article.paragraphs.forEach((text,i)=>{
    const badge=button('本段生词 · 读取中',action(()=>openVocab(i)),'para-badge');badge.id='badge-'+i;
    const paragraph=el('section',{class:'paragraph',id:'para-'+i,'data-index':i},badge,el('p',{},text));
    paragraph.addEventListener('dblclick',action(async event=>{
      if(event.target.closest('button'))return;
      if(reader.pointActive&&reader.touchSelection)return;
      let chosen=selectedQuote();
      if(chosen) {
        chosen=sentenceSelection(reader,chosen);
      }else chosen={quote:text,paragraph:i};
      state.selection=chosen;await analyzeSelection(reader);
    }));body.append(paragraph);
  });
  reader.paragraphNodes=[...body.children];
  article.append(tools,findRow,el('header',{class:'reader-head'},el('span',{class:'eyebrow'},data.bookTitle),
    el('h1',{},data.article.title),data.titleZh?el('h2',{},data.titleZh):null,
    el('div',{class:'reader-meta'},el('span',{},`${data.wordCount.toLocaleString()} 词`),el('span',{},`约 ${Math.max(1,Math.ceil(data.wordCount/220))} 分钟`),
      el('span',{},`${data.article.paragraphs.length} 段`)),
    data.sourceUrl?el('a',{class:'duplicate-source',href:data.sourceUrl,target:'_blank',rel:'noopener noreferrer'},'官方 Transcript ↗'):null,
    data.summaryZh?el('details',{class:'reader-summary'},el('summary',{},data.excerpted?'整期摘要 · 全文分段摘录':'文章摘要'),el('p',{},data.summaryZh)):null),
    el('p',{class:'touch-reading-help',id:'touch-reading-help'}),selection,body);
  view.replaceChildren(article);window.scrollTo(0,0);
  installVocabHover(reader,article);
  installTouchReading(reader,body,pointToggle);
  document.addEventListener('selectionchange',reader.selectionHandler=()=>{if(state.reader===reader)selectionBar(reader);});
  window.addEventListener('resize',reader.selectionResize=()=>positionSelectionBar(reader));
  window.visualViewport?.addEventListener('resize',reader.selectionResize);
  window.visualViewport?.addEventListener('scroll',reader.selectionResize);
  find.oninput=()=>findArticle(reader,find.value);
  const explicitPosition=Number.isInteger(startParagraph)&&startParagraph>=-1&&startParagraph<data.article.paragraphs.length;
  const paragraph=explicitPosition?startParagraph:data.history&&!data.history.atStart?Math.min(data.history.paragraphIndex,data.article.paragraphs.length-1):null;
  if(paragraph!==null&&paragraph>=0) {
    const node=reader.paragraphNodes[paragraph];
    window.scrollTo({top:window.scrollY+node.getBoundingClientRect().top-tools.offsetHeight-8,behavior:'instant'});
    if(explicitPosition)node.classList.add('source-target');
  }
  reader.ready=true;
  startReadingClock(reader,explicitPosition||data.history&&!data.history.atStart?currentPosition(reader):{paragraphIndex:0,paragraphOffset:0});
  updateReaderProgress(reader);
  readerScroll=()=>{
    if(state.reader!==reader)return;
    if(!progressFrame)progressFrame=requestAnimationFrame(()=>{progressFrame=null;updateReaderProgress(reader);positionSelectionBar(reader);});
    progressStatus(reader,'阅读中');clearTimeout(progressTimer);progressTimer=setTimeout(()=>saveProgress(),850);
  };
  window.addEventListener('scroll',readerScroll,{passive:true});
  // Record this article in recent reading even if the user stays at its beginning.
  saveProgress();
  await loadVocab(reader);
}
function resizeReader(reader,delta){reader.hoverHide?.();reader.fontSize=Math.max(16,Math.min(30,reader.fontSize+delta));$('#reader-body').style.setProperty('--reading-size',reader.fontSize+'px');localStorage.setItem('kandu-font',reader.fontSize);updateReaderProgress(reader);}
function findArticle(reader,query) {
  reader.hoverHide?.();
  clearReaderSelection(reader);
  let count=0;const needle=query.toLocaleLowerCase();
  reader.article.paragraphs.forEach((text,i)=>{
    const p=$('#para-'+i+' p');p.replaceChildren();
    if(!needle){p.textContent=text;return;}
    const lower=text.toLocaleLowerCase();let cursor=0,index=lower.indexOf(needle);
    while(index>=0){p.append(text.slice(cursor,index),el('mark',{},text.slice(index,index+needle.length)));cursor=index+needle.length;count++;index=lower.indexOf(needle,cursor);}
    p.append(text.slice(cursor));
  });$('#find-count').textContent=query?`${count} 处匹配`:'';
  updateVocabHighlights(reader);
}
async function loadVocab(reader,{fresh=false}={}) {
  const response=await api(baseFor(reader)+'/vocab',{fresh,onUpdate:value=>applyVocab(reader,value)});
  applyVocab(reader,response);
}
function applyVocab(reader,response){
  if(state.reader!==reader)return;
  if(response.contentVersion&&reader.contentVersion&&response.contentVersion!==reader.contentVersion){cacheNotice('文章正文已更新，刷新后查看新版正文与词表。');return;}
  reader.vocab=response;
  indexVocabHover(reader);
  updateVocabHighlights(reader);
  const count=response.vocab.titleEntries.length+response.vocab.paragraphs.reduce((n,p)=>n+p.entries.length,0);
  $('#vocab-button').textContent=!response.generated?'生成生词表':response.missing.length?`补缺生词表 · ${response.missing.length} 段`:`生词表 ${count}`;
  for(let i=0;i<reader.article.paragraphs.length;i++) {
    const entries=response.vocab.paragraphs.find(p=>p.index===i)?.entries||[];
    $('#badge-'+i).textContent=response.missing.includes(i)?`本段生词 · 待生成${entries.length?' · 已有 '+entries.length+' 项':''}`:`词 ${entries.length} · 本段生词`;
  }
}
function sourceNode(source) {
  if(!source||typeof source!=='object'||!source.articleTitle&&!source.bookTitle) {
    return el('p',{class:'word-source-empty'},'未记录文章来源 · 手动添加或早期收藏');
  }
  const title=source.articleTitle||source.bookTitle;
  const canOpen=typeof source.bookId==='string'&&Number.isInteger(source.articleIndex)&&source.articleIndex>=0;
  const paragraph=Number.isInteger(source.paragraph)?source.paragraph:null;
  const heading=canOpen?el('a',{class:'word-source-link',href:linkFor(source.bookId,source.articleIndex,paragraph),
    onclick:()=>{closeSheet();}},title+' ↗'):el('span',{class:'word-source-link'},title);
  const time=Number(source.recordedAt);
  const when=Number.isFinite(time)&&time>0?new Date(time):null;
  const location=paragraph===-1?'文章标题':paragraph===null?'文章收藏':`第 ${paragraph+1} 段`;
  return el('div',{class:'word-source'},el('div',{class:'word-source-heading'},el('span',{},'最近来源'),
    when?el('time',{datetime:when.toISOString()},when.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})):null),
    heading,el('div',{class:'word-source-meta'},[source.bookTitle,location].filter(Boolean).join(' · ')),
    source.context?el('p',{class:'word-source-context',title:source.context},source.context):null,
    source.contextMeaning?contextMeaningNode(source.contextMeaning,true):null);
}
function entryNode(entry,onSave,source=null) {
  const collect=button(entry.inWordbook?'编辑收藏':'＋ 加入生词本',action(async()=>{
    if(entry.inWordbook){await editWord(entry.wordbookWord||entry.word);return;}
    const value={...entry,word:entry.wordbookWord||entry.word};
    if(source)await api(`/books/${encodeURIComponent(source.bookId)}/articles/${source.index}/collect`,{method:'POST',data:{entry:value,paragraph:source.paragraph}});
    else await api('/wordbook',{method:'POST',data:value});
    entry.inWordbook=true;collect.textContent='编辑收藏';toast('已加入个人生词本');if(onSave)await onSave();
  }));
  return el('div',{class:'vocab-entry'},el('div',{class:'entry-top'},el('div',{},el('span',{class:'vocab-word'},entry.word),
      el('span',{class:'phonetic'},[entry.phonetic?'/'+entry.phonetic.replaceAll('/','')+'/':'',entry.pos||''].filter(Boolean).join('  '))),collect),
    entry.senseSource==='wordbook'?el('small',{class:'muted'},'个人收藏释义'):entry.senseSource==='dictionary'?el('small',{class:'muted'},'本地词典释义 · 可补充本文含义'):entry.senseSource==='phrase_rule'?el('small',{class:'muted'},'本地短语提示 · 可补充本文含义'):null,
    el('p',{class:'entry-sense'},entry.sense||''),entry.inText?el('p',{class:'quote'},entry.inText):null,
    entry.root?el('p',{class:'entry-root'},entry.root):null,entry.note?el('p',{class:'entry-root'},'笔记 · '+entry.note):null);
}
async function openVocab(paragraph) {
  const reader=state.reader;if(!reader)return;
  await loadVocab(reader);if(state.reader!==reader)return;
  // Toolbar starts full generation directly for ungenerated/partial article vocabulary.
  if(paragraph===null&&(!reader.vocab.generated||reader.vocab.missing.length)) {
    await generateReaderVocab(reader,null);return;
  }
  await vocabSheet(reader,paragraph);
}
async function generateReaderVocab(reader,paragraph,request={}) {
  const output=el('div',{},el('p',{class:'muted'},'正在生成生词与短语…'));
  const sheet=showSheet('生词与短语',output,{reader,paragraph});
  const failure=message=>{if(sheet!==state.sheet)return;output.replaceChildren(el('p',{class:'error'},message));finishSheetWait(sheet);};
  try {
    await runJob(baseFor(reader)+'/generate-vocab',request,async job=>{
      if(state.reader!==reader)return;
      if(job.state!=='done'){failure(job.error||'生成已停止，可再次补缺');return;}
      try {
        await loadVocab(reader,{fresh:true});
        if(sheet===state.sheet&&state.reader===reader)await vocabSheet(reader,paragraph);
      }catch(error){failure('生词已生成，加载词表失败：'+error.message);}
    },job=>updateSheetWait(sheet,job.status),()=>failure('连接中断，服务器任务仍会保存结果。可稍后重新打开生词表。'));
  }catch(error){failure(error.message);}
}
async function vocabSheet(reader,paragraph) {
  if(state.reader!==reader)return;
  const data=reader.vocab,body=el('div');
  const missing=paragraph===null?data.missing.length:data.missing.includes(paragraph);
  if(missing)body.append(el('p',{class:'notice'},paragraph===null?`全文还有 ${data.missing.length} 段待生成，当前只展示已有词条和个人收藏。`:'本段生词尚未生成，当前只展示已有词条和个人收藏。'));
  const actions=el('div',{class:'row'},button('生成 / 补缺全文生词',action(()=>generateReaderVocab(reader,paragraph)),'primary'),
    button('补充短语解释',action(()=>generateReaderVocab(reader,paragraph,{phrasesOnly:true,paragraph}))));body.append(actions);
  const search=el('input',{type:'search',class:'search',placeholder:'查找词条…','aria-label':'查找词条'}),list=el('div');
  body.append(el('div',{class:'filters'},search),list);
  function render(){
    list.replaceChildren();let total=0;
    const groups=[...(paragraph===null?[{index:-1,entries:data.vocab.titleEntries}]:[]),...data.vocab.paragraphs.filter(p=>paragraph===null||p.index===paragraph)];
    for(const group of groups){const entries=group.entries.filter(e=>e.word.toLowerCase().includes(search.value.toLowerCase()));if(!entries.length)continue;
      list.append(el('div',{class:'vocab-section'},group.index===-1?'标题生词':`第 ${group.index+1} 段 · ${entries.length} 项`));
      for(const entry of entries){list.append(entryNode(entry,()=>loadVocab(reader,{fresh:true}),{bookId:reader.bookId,index:reader.index,paragraph:group.index}));total++;}
    }
    if(paragraph===null&&data.vocab.idioms?.length){list.append(el('div',{class:'vocab-section'},'熟词僻义'));for(const idiom of data.vocab.idioms)if(idiom.word.toLowerCase().includes(search.value.toLowerCase())){list.append(entryNode({...idiom,sense:idiom.inText,root:'常见义 · '+idiom.common},null,{bookId:reader.bookId,index:reader.index,paragraph:null}));total++;}}
    if(!total)list.append(empty('当前没有可显示的词条',missing?'点击上方按钮生成词表。':'可以补充短语解释，或选中原文查词。'));
  }search.oninput=render;render();showSheet(paragraph===null?'本篇生词与短语':`第 ${paragraph+1} 段 · 生词与短语`,body);
}
async function lookupSelection(reader) {
  const cacheOwner=state.cacheIdentity;
  const chosen={...state.selection};if(!chosen.quote)return;
  const output=el('div',{},el('p',{class:'muted'},'正在查词…'));
  const sheet=showSheet('查词 · 本文含义',output,{reader,paragraph:chosen.paragraph,status:'正在查词…'});
  $('#selection-tools').hidden=true;
  const request={word:chosen.quote,paragraph:chosen.paragraph,start:chosen.start};
  const cacheKey='meaning:'+JSON.stringify([baseFor(reader),reader.contentVersion,state.modelRevision,request]);
  const saved=await ReadingCache.get(cacheKey);
  let response;
  try{response=saved?.value||await api(baseFor(reader)+'/lookup',{method:'POST',data:request});}
  catch(error){if(sheet===state.sheet){output.replaceChildren(el('p',{class:'error'},error.message));finishSheetWait(sheet);}return;}
  if(sheet!==state.sheet)return;
  const context=el('section',{class:'lookup-context','aria-live':'polite'});
  const details=el('details',{class:'lookup-dictionary'},el('summary',{},'词典 / 个人释义 · 已加入个人生词本'),
    entryNode({...response.saved,inWordbook:true,senseSource:'wordbook'}));
  output.replaceChildren(el('div',{class:'lookup-word'},el('strong',{},chosen.quote),el('span',{class:'phonetic'},response.saved.phonetic||'')),
    context,details,sourceNode({...response.saved.source,contextMeaning:null}));
  const refreshVocab=()=>{if(state.reader===reader)return loadVocab(reader,{fresh:true});};
  const explain=async()=>{
    context.replaceChildren(el('p',{class:'muted'},'AI 正在结合当前句子与段落解释…'));
    waitForSheet(sheet,reader,chosen.paragraph,'AI 正在结合本文解释词义…');
    try {
      await runJob(baseFor(reader)+'/lookup-context',{...request,savedWord:response.saved.word},async job=>{
        if(job.state==='done'&&state.csrf&&cacheOwner===state.cacheIdentity)await ReadingCache.put(cacheKey,{...response,contextMeaning:job.result.meaning},{group:baseFor(reader)});
        if(sheet!==state.sheet)return;
        if(job.state==='done'){context.replaceChildren(contextMeaningNode(job.result.meaning));finishSheetWait(sheet);await refreshVocab();}
        else showFailure(job.error||'本文解释未完成');
      },job=>{if(sheet===state.sheet&&job.state!=='done'&&$('p.muted',context)){$('p.muted',context).textContent=job.status;updateSheetWait(sheet,job.status);}},()=>{if(sheet===state.sheet)showFailure('连接中断，服务器仍会保存结果，请稍后重试。');});
    }catch(error){if(sheet===state.sheet)showFailure(error.message);}
  };
  const showFailure=message=>{details.open=true;context.replaceChildren(el('p',{class:'muted'},'本文含义暂未生成：'+message),button('重试本文解释',action(explain)));finishSheetWait(sheet);};
  if(response.contextMeaning){context.append(contextMeaningNode(response.contextMeaning));finishSheetWait(sheet);}
  else if(response.canExplain)await explain();
  else {details.open=true;context.append(el('p',{class:'muted'},'配置 LLM 后，可结合本文语境解释词义。'),el('a',{href:'#/settings'},'前往设置 →'));finishSheetWait(sheet);}
  if(!saved){if(response.contextMeaning&&state.csrf&&cacheOwner===state.cacheIdentity)await ReadingCache.put(cacheKey,response,{group:baseFor(reader)});await refreshVocab();}
  else{
    // Showing a cached explanation is instant; collection/source updates still
    // happen on the server and are never falsely reported as a new offline save.
    $('summary',details).textContent='词典 / 个人释义 · 已有收藏，正在核对';
    api(baseFor(reader)+'/lookup',{method:'POST',data:request}).then(async value=>{
      if(state.csrf&&cacheOwner===state.cacheIdentity)await ReadingCache.put(cacheKey,value,{group:baseFor(reader)});
      if(sheet===state.sheet)$('summary',details).textContent='词典 / 个人释义 · 已加入个人生词本';
      await refreshVocab();
    }).catch(()=>{if(sheet===state.sheet)$('summary',details).textContent='本机缓存 · 收藏与来源暂未更新';});
  }
}
function contextMeaningNode(meaning,compact=false) {
  return el('section',{class:'context-meaning'+(compact?' compact':'')},
    el('div',{class:'context-meaning-heading'},el('span',{},'AI · 本文含义'),meaning.pos?el('small',{},meaning.pos):null),
    el('p',{class:'context-sense'},meaning.sense),el('p',{class:'context-explanation'},meaning.explanation),
    !compact&&meaning.sentence?el('p',{class:'quote'},meaning.sentence):null,
    !compact&&meaning.translation?el('p',{class:'context-translation'},meaning.translation):null);
}
async function analyzeSelection(reader) {
  const cacheOwner=state.cacheIdentity;
  const chosen={...state.selection};if(!chosen.quote)return;
  const output=el('div',{},el('p',{class:'quote'},chosen.quote),el('p',{class:'muted'},'正在准备句子讲解…'));
  const sheet=showSheet('句子讲解',output,{reader,paragraph:chosen.paragraph,status:'正在准备句子讲解…'});
  const cacheKey='analysis:'+JSON.stringify([baseFor(reader),reader.contentVersion,state.modelRevision,chosen.quote]);
  const cached=await ReadingCache.get(cacheKey);
  if(sheet!==state.sheet)return;
  if(cached){renderAnalysis(output,cached.value,chosen.quote,{bookId:reader.bookId,index:reader.index,paragraph:chosen.paragraph});finishSheetWait(sheet);return;}
  const failure=message=>{if(sheet!==state.sheet)return;output.replaceChildren(el('p',{class:'quote'},chosen.quote),el('p',{class:'error'},message));finishSheetWait(sheet);};
  try {
    await runJob(baseFor(reader)+'/analyze',{sentence:chosen.quote,paragraph:chosen.paragraph},async job=>{
      if(job.state==='done'&&state.csrf&&cacheOwner===state.cacheIdentity)await ReadingCache.put(cacheKey,job.result,{group:baseFor(reader)});
      if(sheet!==state.sheet)return;
      if(job.state!=='done'){failure(job.error||'未能完成讲解');return;}
      renderAnalysis(output,job.result,chosen.quote,{bookId:reader.bookId,index:reader.index,paragraph:chosen.paragraph});
      finishSheetWait(sheet);
    },job=>{if(sheet===state.sheet&&job.state!=='done'){$('p.muted',output).textContent=job.status;updateSheetWait(sheet,job.status);}},
      ()=>failure('连接中断，服务器仍会保存结果。可稍后重新打开句子讲解。'));
  }catch(error){failure(error.message);}
}
function renderAnalysis(output,data,sentence,source=null) {
  output.replaceChildren(el('div',{class:'analysis-sentence'},sentence));
  if(data.grammarComponents?.length){const section=el('section',{class:'analysis-block'},el('h3',{},'句子结构与语法'));
    const components=(nodes,depth=0)=>{for(const c of nodes){const row=el('div',{class:'analysis-component'},el('span',{},c.text),el('small',{},c.label));if(depth)row.classList.add('secondary');section.append(row);if(c.children)components(c.children,depth+1);}};
    components(data.grammarComponents);output.append(section);
  }else if(data.grammarDiagram)output.append(el('details',{},el('summary',{},'查看原语法图解'),el('pre',{},data.grammarDiagram)));
  for(const point of data.grammarPoints||[])output.append(el('section',{class:'analysis-block'},el('h3',{},point.title),el('p',{},point.text)));
  for(const [key,title]of [['context','语境与含义'],['translation','参考译文']])if(data[key])output.append(el('section',{class:'analysis-block'},el('h3',{},title),el('p',{},data[key])));
  if(data.vocab?.length){output.append(el('div',{class:'vocab-section'},'句中词汇'));for(const entry of data.vocab)output.append(entryNode(entry,null,source));}
}

async function discussionSheet(reader,selection=null) {
  const data=await api(baseFor(reader)+'/discussion');
  if(state.reader!==reader)return;
  const messages=el('div',{class:'discussion-list'}),status=el('span',{class:'muted'});
  const messageNode=(m)=>el('div',{class:'message '+m.role},el('small',{},m.role==='user'?'你':'刊读 AI'),
    m.quote?el('p',{class:'quote'},m.quote):null,el('div',{},m.content|| (m.status==='pending'?'等待回答…':'回答未完成')),m.error?el('p',{class:'error'},m.error):null);
  for(const message of data.messages)messages.append(messageNode(message));
  if(!data.messages.length)messages.append(el('p',{class:'muted'},'围绕作者观点、句意或背景提问。会话随文章保存，下次可继续。'));
  const input=el('textarea',{placeholder:'关于这篇文章，你想了解什么？','aria-label':'向 AI 提问',rows:3});
  const submit=el('button',{type:'submit',class:'primary'},'发送问题 →'),stop=button('停止回答',action(async()=>{if(jobId)await api('/jobs/'+jobId+'/stop',{method:'POST',data:{}});}));stop.hidden=true;
  const quote=selection?el('p',{class:'quote'},selection.quote):null;
  const form=el('form',{class:'discussion-input'},quote,input,el('div',{class:'row'},submit,stop,status));
  let jobId=null;
  const sheet=showSheet('与 AI 讨论',el('div',{},messages,form));
  messages.scrollTop=messages.scrollHeight;
  form.onsubmit=action(async event=>{
    event.preventDefault();const question=input.value.trim();if(!question||submit.disabled)return;
    submit.disabled=true;
    const user=messageNode({role:'user',content:question,quote:selection?.quote});
    const answer=el('div',{class:'message assistant'},el('small',{},'刊读 AI'),el('div',{class:'answer-text'},'等待回答…'));
    messages.append(user,answer);messages.scrollTop=messages.scrollHeight;
    waitForSheet(sheet,reader,selection?.paragraph,'正在提交问题…');
    const failure=message=>{if(sheet!==state.sheet)return;stop.hidden=true;submit.disabled=false;status.textContent=message;finishSheetWait(sheet);};
    try {
      jobId=await runJob(baseFor(reader)+'/discussion',{question,quote:selection?.quote||'',paragraph:selection?.paragraph},async job=>{
        if(sheet!==state.sheet)return;
        stop.hidden=true;submit.disabled=false;
        if(job.state==='error'){$('.answer-text',answer).textContent=job.text||job.error;status.textContent='回答未完成，可重新提问';}
        else status.textContent=job.state==='stopped'?'已停止，已收到的内容已保存':'会话已保存';
        finishSheetWait(sheet);
      },job=>{
        if(sheet!==state.sheet)return;
        status.textContent=job.status;
        updateSheetWait(sheet,job.status);
        if(job.text){$('.answer-text',answer).textContent=job.text;messages.scrollTop=messages.scrollHeight;}
      },()=>failure('连接中断，服务器仍会保存会话，请稍后重新打开。'));
      input.value='';stop.hidden=false;
    }catch(error){user.remove();answer.remove();submit.disabled=false;failure(error.message);}
  });
  input.onkeydown=event=>{if(event.ctrlKey&&event.key==='Enter'){event.preventDefault();form.requestSubmit();}};
}

async function wordbookPage(token) {
  const data=await api('/wordbook');if(token!==state.route)return;
  const search=el('input',{class:'search',type:'search',placeholder:'搜索单词、释义、来源…','aria-label':'搜索个人生词本'}),list=el('div',{class:'wordbook-list'});
  const render=()=>{const q=search.value.toLowerCase();const found=data.words.filter(w=>[w.word,w.sense,w.note,w.source?.articleTitle,w.source?.bookTitle,w.source?.context].join(' ').toLowerCase().includes(q));
    list.replaceChildren(...found.map(w=>{
      const entry=entryNode({...w,inWordbook:true});
      entry.append(sourceNode(w.source));
      $('.entry-top',entry).append(button('删除',action(async()=>{if(!confirm('从个人生词本删除 '+w.word+'？'))return;
        await api('/wordbook/'+encodeURIComponent(w.word),{method:'DELETE'});data.words=data.words.filter(v=>v.word!==w.word);render();toast('已删除');})));
      return entry;
    }));if(!found.length)list.append(empty('暂无词条','阅读时选中单词查词，会自动收藏到这里。'));
  };
  search.oninput=render;view.replaceChildren(heading('YOUR GROWING VOCABULARY','个人生词本',`${data.words.length} 个词条 · 各设备共用`,[button('＋ 添加词条',()=>editWord(),'primary')]),el('div',{class:'filters'},search),list);render();
}
async function editWord(word='') {
  let initial={word};if(word){const data=await api('/wordbook?q='+encodeURIComponent(word));initial=data.words.find(w=>w.word===word)||initial;}
  const fields={};const form=el('form',{class:'form-stack'});
  for(const [key,label]of [['word','单词 / 短语'],['phonetic','音标'],['pos','词性'],['sense','释义'],['root','词根 / 用法'],['note','个人笔记']]){
    const input=el(['sense','root','note'].includes(key)?'textarea':'input',{value:['sense','root','note'].includes(key)?null:initial[key]||'',required:key==='word'});
    if(input.tagName==='TEXTAREA')input.value=initial[key]||'';
    fields[key]=input;form.append(el('label',{},label,input));
  }
  if(word)form.append(sourceNode(initial.source));
  form.append(el('button',{type:'submit',class:'primary'},'保存词条'));
  form.onsubmit=action(async event=>{event.preventDefault();const data=Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,v.value]));
    if(word)data.original=word;await api('/wordbook',{method:'POST',data});closeSheet();toast('词条已保存');
    if(state.reader)await loadVocab(state.reader,{fresh:true});else if(location.hash==='#/wordbook')route();});
  showSheet(word?'编辑个人词条':'添加个人词条',form);
}

async function settingsPage(token) {
  const [data,sync,access]=await Promise.all([api('/preferences'),api('/reading-sync'),api('/settings/access')]);if(token!==state.route)return;
  const level=el('select',{},...Object.entries({cet4:'四级 CET-4',cet6:'六级 CET-6',toefl:'TOEFL',gre:'GRE'}).map(([k,v])=>el('option',{value:k},v)));level.value=data.vocabLevel;
  level.onchange=action(async()=>{await api('/preferences',{method:'POST',data:{vocabLevel:level.value}});toast('词汇水平已保存');});
  const modelPanels=el('div',{class:'settings-grid config-panels'});
  view.replaceChildren(heading('MAKE IT YOURS','阅读设置','模型配置由独立密码保护，各设备共用阅读偏好'),
    el('section',{class:'panel'},el('h2',{},'阅读偏好'),el('label',{},'默认已掌握的词汇',level)),modelPanels,readingCachePanel(),syncSettingsPanel(sync));
  if(!access.unlocked){
    const password=el('input',{type:'password',required:true,maxlength:200,autocomplete:'current-password','aria-label':'模型配置独立密码'});
    const error=el('p',{class:'error',role:'alert',hidden:true});
    const submit=el('button',{type:'submit',class:'primary',disabled:!access.configured},'解锁模型配置');
    const form=el('form',{class:'form-stack config-unlock-form'},el('label',{},'独立密码',password),error,submit);
    form.onsubmit=async event=>{
      event.preventDefault();submit.disabled=true;error.hidden=true;
      try {await api('/settings/unlock',{method:'POST',data:{password:password.value}});if(token===state.route)await route();}
      catch(failure){error.textContent=failure.message;error.hidden=false;}
      finally {password.value='';submit.disabled=false;}
    };
    modelPanels.append(el('section',{class:'panel config-locked'},el('h2',{},'模型配置已锁定'),
      el('p',{class:'muted'},access.configured?'查看、编辑和导入 LLM 配置需要独立密码，解锁有效期为 15 分钟。':'请先在服务器运行 tools/set_config_password.py 设置独立密码。'),form));
    return;
  }
  const settings=await api('/settings');if(token!==state.route)return;
  state.settingsTimer=setTimeout(()=>{if(token===state.route)route();},Math.max(0,access.expiresAt-Date.now())+100);
  const models=el('div');
  for(const item of settings.models)models.append(el('div',{class:'model-item'},el('div',{},item.name,el('small',{},item.provider+' · '+item.model)),
    el('div',{class:'model-actions'},button('编辑',()=>editModel(item)),
      button(item.name===settings.defaultModel?'默认模型':'设为默认',action(async()=>{await api('/settings',{method:'POST',data:{defaultModel:item.name}});route();}),item.name===settings.defaultModel?'secondary':''))));
  if(!settings.models.length)models.append(el('p',{class:'muted'},'尚未配置模型。已有的阅读结果仍可浏览。'));
  const name=el('input',{placeholder:'配置名称（可留空）'}),json=el('textarea',{placeholder:'粘贴完整 cc-switch / Claude Code / OpenAI 配置 JSON',rows:7,spellcheck:false,autocomplete:'off'});
  const form=el('form',{class:'form-stack'},el('label',{},'名称',name),el('label',{},'配置 JSON',json),
    el('small',{class:'muted'},'密钥只保存在服务器，不会回传到页面。'),el('button',{type:'submit',class:'primary'},'保存并设为默认'));
  form.onsubmit=action(async event=>{event.preventDefault();await api('/settings',{method:'POST',data:{name:name.value,configJson:json.value}});json.value='';toast('模型配置已保存');route();});
  modelPanels.append(el('section',{class:'panel'},el('h2',{},'模型配置'),
    el('p',{class:'muted'},'已临时解锁，到期后自动锁定。'),
    button('立即锁定模型配置',action(async()=>{await api('/settings/lock',{method:'POST',data:{}});route();})),models),
    el('section',{class:'panel'},el('h2',{},'导入模型配置'),form));
}

function readingCachePanel(){
  const details=el('p',{class:'muted'},'正在检查本机缓存…');
  const panel=el('section',{class:'panel'},el('h2',{},'本机阅读缓存'),details,
    el('p',{class:'muted'},'最多保存最近 20 篇文章及相关词表、讲解，容量上限 50 MB。优先显示本机内容，后台核对更新；缓存只属于当前浏览器。'),
    el('div',{class:'row'},button('立即同步阅读记录',action(async()=>{
      await ReadingSync.drain();await refresh();toast(ReadingSync.pending()?'还有记录等待同步':'阅读记录已同步');
    })),button('清空阅读缓存',action(async()=>{
      cacheEpoch++;await ReadingCache.clear();await refresh();toast('本机阅读缓存已清空，未同步记录和服务器数据保留');
    }))));
  async function refresh(){const info=await ReadingCache.info();
    details.textContent=`已缓存 ${info.articles} 篇 · ${(info.bytes/1024/1024).toFixed(1)} / 50 MB · ${ReadingSync.pending()} 条阅读记录待同步`+
      (info.persistent?'':' · 浏览器持久缓存不可用，当前使用临时缓存');}
  refresh();return panel;
}

function syncSettingsPanel(initial) {
  const status=el('p',{class:'muted'},initial.enabled?'同步已启用。可在 Chrome 插件中上传已读文章。':'尚未开启插件阅读同步。');
  const output=el('div',{class:'form-stack',hidden:true});
  const panel=el('section',{class:'panel sync-panel'},el('h2',{},'Chrome 插件阅读同步'),
    el('p',{class:'muted'},'把网页文章的标题、原文链接、总词数和完成时间汇总到已读统计。文章正文和模型密钥不会上传。'),status,
    el('div',{class:'row'},button('生成 / 重置同步码',action(async()=>{
      if(initial.enabled&&!confirm('重置后，已连接的插件需要填写新同步码。继续？'))return;
      const {token}=await api('/reading-sync',{method:'POST',data:{}});initial.enabled=true;
      status.textContent='同步码仅在此次生成后显示，请复制到插件设置。此码只能上传阅读记录。';
      const server=el('input',{value:location.origin,readonly:true,'aria-label':'Web 同步地址'});
      const code=el('input',{value:token,readonly:true,'aria-label':'阅读同步码',autocomplete:'off',spellcheck:false});
      output.replaceChildren(el('label',{},'Web 地址',server),el('label',{},'同步码',code),
        button('复制同步码',action(async()=>{await navigator.clipboard.writeText(token);toast('同步码已复制');})));output.hidden=false;
    }),'primary'),button('停用同步码',action(async()=>{
      await api('/reading-sync',{method:'DELETE'});initial.enabled=false;output.replaceChildren();output.hidden=true;
      status.textContent='同步已停用，已上传的阅读记录保留。';
    }))),output);
  return panel;
}

async function readingStatsPage(token) {
  const data=await api('/reading-stats?days=365&timezoneOffset='+new Date().getTimezoneOffset());if(token!==state.route)return;
  const search=el('input',{type:'search',class:'search',placeholder:'搜索标题、刊物或网站…','aria-label':'搜索已读文章'});
  const list=el('div',{class:'completed-list'}),count=el('span',{class:'count'});
  const render=()=>{
    const query=search.value.trim().toLocaleLowerCase();
    const found=data.articles.filter(a=>[a.title,...a.sources.map(s=>s.source)].join(' ').toLocaleLowerCase().includes(query));
    count.textContent=`${found.length} 篇`;
    list.replaceChildren(...found.map(a=>el('article',{class:'completed-card'},
      el('div',{class:'completed-heading'},el('h2',{},a.title),el('strong',{},a.wordCount?`${a.wordCount.toLocaleString()} 词`:'词数待补全')),
      el('div',{class:'completed-meta'},el('time',{datetime:new Date(a.readAt).toISOString()},new Date(a.readAt).toLocaleDateString()),
        el('span',{},a.sources.length>1?`${a.sources.length} 个出处 · 标题去重后计为 1 篇`:'已读文章'),
        el('span',{class:'article-reading-time'},a.timing.activeMs?`有效阅读 ${readingDuration(a.timing.activeMs)}`:'未计时'),
        el('span',{class:'article-reading-speed'},a.timing.wordsPerMinute!==null?`约 ${a.timing.wordsPerMinute} 词/分钟`:'速度样本不足')),
      a.timing.activeMs?el('div',{class:'count'},`计时期间约阅读 ${a.timing.measuredWords.toLocaleString()} 词；估算速度包含停留阅读时间，跳转及重复回滚不累计前进词数。`):null,
      el('div',{class:'completed-sources'},...a.sources.map(s=>el('a',s.kind==='chrome'?{href:s.url,target:'_blank',rel:'noopener noreferrer'}:
        {href:linkFor(s.bookId,s.articleIndex)},`${s.kind==='chrome'?'网页':'刊物'} · ${s.source}${s.kind==='chrome'?' ↗':' →'}`))))));
    if(!found.length)list.append(empty(query?'没有匹配的已读文章':'你的阅读积累，从这里开始',
      '读到文章结尾或标记为已读，即可加入；Chrome 网页文章也可以同步到这里。'));
  };
  search.oninput=render;
  const analytics=data.analytics,total=analytics.totals;
  view.replaceChildren(heading('YOUR READING JOURNEY','已读统计','看见每天的积累，也看见阅读节奏的变化。'),
    el('div',{class:'reading-totals'},el('div',{},el('strong',{'data-stat':'articles'},data.articleCount.toLocaleString()),el('span',{},'已读文章 · 按标题去重')),
      el('div',{},el('strong',{'data-stat':'words'},data.totalWords.toLocaleString()),el('span',{},'累计已读词数')),
      el('div',{},el('strong',{'data-stat':'active-time'},readingDuration(total.activeMs)),el('span',{},'有效阅读时长')),
      el('div',{},el('strong',{'data-stat':'speed'},total.wordsPerMinute===null?'—':`${total.wordsPerMinute} 词/分`),el('span',{},'平均估算速度 · 按总时间加权')),
      el('div',{},el('strong',{'data-stat':'active-days'},`${analytics.activeDays} 天`),el('span',{},'计时阅读天数')),
      el('div',{},el('strong',{'data-stat':'streak'},`${analytics.currentStreak} 天`),el('span',{},'当前连续阅读'))),
    el('p',{class:'count'},'标题忽略大小写、空白和标点差异；同标题各出处合并，词数取最大值，不重复累加。'),
    readingAnalyticsPanel(analytics),
    el('div',{class:'filters'},search,count),list);render();
}

function editModel(initial) {
  const name=el('input',{value:initial.name,required:true,maxlength:80,autocomplete:'off'});
  const provider=el('select',{'aria-label':'接口类型'},el('option',{value:'openai'},'OpenAI 兼容'),el('option',{value:'anthropic'},'Anthropic 兼容'));
  provider.value=initial.provider;
  const baseUrl=el('input',{type:'url',value:initial.baseUrl,required:true,maxlength:4000,spellcheck:false});
  const model=el('input',{value:initial.model,required:true,maxlength:200,spellcheck:false});
  const key=el('input',{type:'password',autocomplete:'new-password',spellcheck:false,maxlength:8192,
    required:!initial.hasApiKey,placeholder:initial.hasApiKey?'已保存，留空保留原密钥':'输入 API Key'});
  const submit=el('button',{type:'submit',class:'primary'},'保存模型配置');
  const error=el('p',{class:'error',role:'alert',hidden:true});
  const form=el('form',{class:'form-stack'},el('label',{},'配置名称',name),el('label',{},'接口类型',provider),
    el('label',{},'API 地址',baseUrl),el('label',{},'模型名称',model),el('label',{},'API Key',key),
    el('small',{class:'muted'},'原密钥不会回传到页面。留空保留，填写新密钥则替换。'),error,
    el('div',{class:'row'},submit,button('取消',()=>closeSheet())));
  form.onsubmit=async event=>{
    event.preventDefault();submit.disabled=true;error.hidden=true;
    try {
      await api('/settings',{method:'POST',data:{originalName:initial.name,modelConfig:{name:name.value,
        provider:provider.value,baseUrl:baseUrl.value,model:model.value,apiKey:key.value}}});
      key.value='';closeSheet();toast('模型配置已更新');await route();
    } catch(failure) {error.textContent=failure.message;error.hidden=false;}
    finally {submit.disabled=false;}
  };
  showSheet('编辑模型配置',form);
}

async function route({refresh=false}={}) {
  clearTimeout(toastTimer);$('#toast').hidden=true;
  view.querySelectorAll('img').forEach(img=>img.coverObserver?.disconnect());
  state.reader?.touchCleanup?.();
  state.reader?.hoverCleanup?.();
  clearTimeout(state.settingsTimer);
  if(!state.csrf){showLogin();return;}
  state.statsCleanup?.();state.statsCleanup=null;
  if(state.reader){saveProgress(true);stopReadingClock(state.reader);document.removeEventListener('selectionchange',state.reader.selectionHandler);
    window.removeEventListener('resize',state.reader.selectionResize);
    window.visualViewport?.removeEventListener('resize',state.reader.selectionResize);
    window.visualViewport?.removeEventListener('scroll',state.reader.selectionResize);}
  clearTimeout(progressTimer);cancelAnimationFrame(progressFrame);progressFrame=null;if(readerScroll)window.removeEventListener('scroll',readerScroll);readerScroll=null;
  state.reader=null;state.selection=null;
  closeSheet();
  const token=++state.route;$('#navigation').hidden=false;
  state.forceReadingRefresh=refresh;
  const [path,query='']=(location.hash.slice(1)||'/library').split('?');
  const parts=path.split('/').filter(Boolean).map(decodeURIComponent);
  const requestedParagraph=new URLSearchParams(query).get('paragraph');
  const startParagraph=requestedParagraph!==null&&/^-?\d+$/.test(requestedParagraph)?Number(requestedParagraph):null;
  view.replaceChildren(el('div',{class:'loading'},'正在打开…'));window.scrollTo(0,0);
  try {
    if(parts[0]==='book'&&parts[1])await digestPage(parts[1],token);
    else if(parts[0]==='read'&&parts[1]&&/^\d+$/.test(parts[2]))await readerPage(parts[1],Number(parts[2]),token,startParagraph);
    else if(parts[0]==='wordbook')await wordbookPage(token);
    else if(parts[0]==='settings')await settingsPage(token);
    else if(parts[0]==='reading-stats')await readingStatsPage(token);
    else await library(token);
  }catch(error){if(token===state.route&&state.csrf)view.replaceChildren(empty('暂时无法打开',error.message),button('重试',()=>route({refresh:true}),'primary'));}
  finally{if(token===state.route)state.forceReadingRefresh=false;}
}
window.addEventListener('hashchange',route);
window.addEventListener('pagehide',()=>{readingClockTick();flushReadingClock(state.reader,true);saveProgress(true);});
document.addEventListener('visibilitychange',()=>{readingClockTick();if(document.hidden){flushReadingClock(state.reader,true);saveProgress(true);}else if(state.reader?.clock)state.reader.clock.lastInteraction=performance.now();});
(async()=>{try{const session=await api('/session');if(session.authenticated){state.csrf=session.csrf;await initReadingStorage(session);await route();}else{ReadingSync.reset();await ReadingCache.init('');await ReadingCache.clear();showLogin();}}catch(error){view.replaceChildren(empty('连接服务器失败',error.message));}})();
