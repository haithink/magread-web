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
const state = {csrf:'', route:0, reader:null, selection:null, activeJobs:new Map(), jobObservers:new Map(), sheet:0};
const view = $('#app');
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
async function api(path, {method = 'GET', data, form, keepalive = false} = {}) {
  const headers = {};
  if (method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  if (data !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api' + path, {method, headers, credentials:'same-origin',
    body:form || (data !== undefined ? JSON.stringify(data) : undefined), keepalive});
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== '/login') showLogin();
    throw new Error(result.error || '请求未完成');
  }
  return result;
}
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
function showSheet(title, content) {
  state.sheet += 1;
  $('#sheet-title').textContent = title;
  $('#sheet-content').replaceChildren(content);
  if (!$('#sheet').open) $('#sheet').showModal();
  return state.sheet;
}
$('#sheet-close').onclick = () => $('#sheet').close();
$('#sheet').addEventListener('click', event => { if (event.target === $('#sheet')) { const r=$('#sheet').getBoundingClientRect(); if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom) $('#sheet').close(); } });
$('#sheet').addEventListener('close', () => { state.sheet += 1; });
$('#logout').onclick = action(async () => { await api('/logout', {method:'POST', data:{}}); state.csrf=''; state.reader=null; showLogin(); });

function showLogin() {
  $('#navigation').hidden = true;
  if ($('#sheet').open) $('#sheet').close();
  view.replaceChildren($('#login-template').content.cloneNode(true));
  $('#login-form').onsubmit = async event => {
    event.preventDefault(); const submit=$('button', event.target); submit.disabled=true;
    try {
      const response=await api('/login', {method:'POST', data:{password:$('#password').value}});
      state.csrf=response.csrf; await route();
    } catch(error) { $('#login-error').textContent=error.message; }
    finally { submit.disabled=false; }
  };
}

function displayJobBar() {
  $('#jobbar').hidden=state.activeJobs.size===0;
  if(state.activeJobs.size) $('#job-status').textContent=[...state.activeJobs.values()].map(j=>j.status).join(' · ');
}
async function runJob(path, data={}, onDone, onUpdate) {
  const {jobId}=await api(path,{method:'POST',data});
  if(state.activeJobs.has(jobId)) {toast('这个任务已经在进行中');return jobId;}
  return watchJob(jobId,onDone,onUpdate);
}
function watchJob(jobId,onDone,onUpdate) {
  if(!state.jobObservers.has(jobId))state.jobObservers.set(jobId,[]);
  state.jobObservers.get(jobId).push({onDone,onUpdate});
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
        for(const observer of observers)if(observer.onDone)await observer.onDone(job);
        return;
      }
      setTimeout(poll,650);
    } catch(error) {
      state.activeJobs.delete(jobId);state.jobObservers.delete(jobId); displayJobBar(); toast('连接中断，服务器任务仍会保存结果。'+error.message);
    }
  };
  poll(); return jobId;
}

async function library(token) {
  const [data,stats]=await Promise.all([api('/library'),api('/reading-stats')]); if(token!==state.route)return;
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
    result.replaceChildren();
    if(!filtered.length) result.append(empty(data.books.length?'没有匹配的刊物':'从一本刊物开始','添加 EPUB 或 PDF，已有摘要和词表会自动复用。'));
    for(const [name,books] of groups) {
      books.sort((a,b)=>a.sourceType==='podcast'&&b.sourceType==='podcast'?b.episodeNumber-a.episodeNumber:b.fileName.localeCompare(a.fileName,undefined,{numeric:true}));
      const grid=el('div',{class:'book-grid'});
      for(const book of books) {
        const isPodcast=book.sourceType==='podcast';
        const cover=book.cover?el('img',{src:`/api/books/${encodeURIComponent(book.id)}/cover`,alt:book.title||book.fileName,loading:'lazy'}):isPodcast?
          el('span',{class:'podcast-cover'},el('small',{},name.replace(/ Podcast$/i,'').toUpperCase()),el('strong',{},'#'+book.episodeNumber),el('span',{},'PODCAST'),el('small',{},book.publishedAt||'TRANSCRIPT')):el('span',{class:'cover-placeholder'},name);
        const articleCount=book.articleCountKnown?`${book.articleCount} 篇文章`:'待解析文章';
        const summaryCount=book.articleCountKnown&&book.articleCount===0?'本期均为重复文章':book.summaryCount?`${book.summaryCount} 篇已有摘要`:'待生成摘要';
        grid.append(el('article',{class:'book-card'},el('a',{class:'cover-link',href:`#/book/${encodeURIComponent(book.id)}`},cover,
          el('span',{class:'format-tag'},isPodcast?'TRANSCRIPT':book.ext.toUpperCase())),
          el('h3',{},el('a',{href:`#/book/${encodeURIComponent(book.id)}`},book.title||book.fileName.replace(/\.(epub|pdf)$/i,''))),
          el('div',{class:'count'},book.available?`${articleCount} · ${summaryCount}`:'原文件暂不可用')));
      }
      result.append(el('section',{class:'library-section'},el('div',{class:'section-title'},el('h2',{},name),el('span',{class:'count'},`${books.length} 本`)),grid));
    }
  }
  search.oninput=renderBooks;select.onchange=renderBooks;
  content.append(el('div',{class:'filters'},search,select),result);view.replaceChildren(content);renderBooks();
}

async function podcastSheet() {
  const data=await api('/podcast-sources');
  const body=el('div',{class:'form-stack'},el('p',{class:'muted'},'从官方网站导入文字 Transcript，每期作为一本书。自动使用默认 LLM 生成中文摘要，暂不生成生词表；再次导入会跳过已有内容并补缺。'));
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
      el('p',{class:'count'},'保存文字并在本地生成简洁封面。长篇摘要采用覆盖全文的分段摘录。'),
      status,progress,failures,el('div',{class:'row'},start,stop)));
    if(running&&source.jobId)watchJob(source.jobId,done,render);
    render({status:source.status.status,result:source.status});
  }
}

function uploadSheet() {
  const file=el('input',{type:'file',accept:'.epub,.pdf','aria-label':'选择刊物'});
  const form=el('form',{class:'form-stack upload-form'},el('p',{class:'muted'},'添加 EPUB 或 PDF（最大 150 MB）。摘要可在文章目录中生成。'),file,
    el('button',{type:'submit',class:'primary'},'上传并加入书库'));
  form.onsubmit=action(async event=>{
    event.preventDefault();if(!file.files.length)return;
    if(file.files[0].size>150*1024**2)throw new Error('文件超过 150 MB');
    const submit=$('button',form);submit.disabled=true;
    try {
      const body=new FormData();body.append('file',file.files[0]);
      const {jobId}=await api('/books',{method:'POST',form:body});
      $('#sheet').close();
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
async function digestPage(bookId,token) {
  const data=await api('/books/'+encodeURIComponent(bookId));if(token!==state.route)return;
  const q=el('input',{class:'search',type:'search',placeholder:'搜索标题、摘要…','aria-label':'搜索文章'});
  let filter='all';const grid=el('div',{class:'article-grid'}),tabs=el('div',{class:'tabs'});
  const duplicateCount=data.articles.filter(a=>a.earlierOccurrence).length;
  const showDuplicates=el('input',{type:'checkbox',id:'show-duplicate-articles'});
  const issueCount=el('span',{class:'count',id:'issue-article-count','aria-live':'polite'});
  const visibleArticles=()=>data.articles.filter(a=>showDuplicates.checked||!a.earlierOccurrence);
  const counts=articles=>Object.fromEntries(['all',...Object.keys(labels)].map(k=>[k,k==='all'?articles.length:articles.filter(a=>a.state===k).length]));
  const cards=new Map();
  const stateSelect=(a)=>{
    const select=el('select',{'aria-label':'文章标记'},...Object.entries(labels).map(([k,v])=>el('option',{value:k},v)));
    select.value=a.state;
    select.onchange=action(async()=>{const previous=a.state;select.disabled=true;
      try{await api(`/books/${encodeURIComponent(bookId)}/state`,{method:'POST',data:{articleIndex:a.index,state:select.value}});a.state=select.value;
        $('.state-pill',cards.get(a.index)).textContent=labels[a.state];render();}
      catch(error){select.value=previous;throw error;}finally{select.disabled=false;}
    });return select;
  };
  for(const a of data.articles) {
    const card=el('article',{class:'article-card'},
      el('div',{class:'card-top'},el('span',{},`ARTICLE ${String(a.index+1).padStart(2,'0')}`),el('span',{class:'state-pill'},labels[a.state])),
      el('a',{href:linkFor(bookId,a.index)},el('h2',{},a.titleEn),a.titleZh?el('h3',{},a.titleZh):null),
      el('p',{},a.summaryZh||'这篇文章尚未生成摘要，可以直接打开原文阅读。'),
      a.earlierOccurrence?el('a',{class:'duplicate-source',href:linkFor(a.earlierOccurrence.bookId,a.earlierOccurrence.articleIndex)},
        '更早收录于 '+a.earlierOccurrence.fileName.replace(/\.(epub|pdf)$/i,'')+' ↗'):null,
      el('div',{class:'card-bottom'},stateSelect(a),el('a',{href:linkFor(bookId,a.index)},a.history?`继续阅读 ${Math.round(a.history.progress*100)}% →`:'打开文章 →')));
    cards.set(a.index,card);
  }
  const tabButtons=new Map();for(const [key,label] of [['all','全部'],...Object.entries(labels)]){
    const b=button(label,()=>{filter=key;render();});tabButtons.set(key,b);tabs.append(b);
  }
  function render(){
    const articles=visibleArticles(),c=counts(articles);
    issueCount.textContent=duplicateCount?`共 ${data.articles.length} 篇 · ${showDuplicates.checked?'包含 '+duplicateCount+' 篇重复文章':'已隐藏 '+duplicateCount+' 篇重复文章'}`:`${data.articles.length} 篇文章`;
    for(const [k,b]of tabButtons){b.textContent=`${k==='all'?'全部':labels[k]} ${c[k]}`;b.classList.toggle('active',k===filter);}
    const terms=q.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const matches=articles.filter(a=>(filter==='all'||a.state===filter)&&terms.every(t=>(a.titleEn+' '+a.titleZh+' '+a.summaryZh).toLowerCase().includes(t)));
    grid.replaceChildren(...matches.map(a=>cards.get(a.index)));
    if(!matches.length)grid.append(!articles.length&&duplicateCount&&!showDuplicates.checked?
      empty('本期文章均已在更早刊物出现','勾选“显示重复文章”可查看原目录，摘要和阅读记录仍保留。'):empty('没有匹配的文章'));
  }
  q.oninput=render;showDuplicates.onchange=render;
  view.replaceChildren(el('a',{href:'#/library',class:'back'},'← 我的书库'),
    el('div',{class:'digest-head'},heading('ISSUE CONTENTS',data.title||data.fileName.replace(/\.(epub|pdf)$/i,''),issueCount,[
      button('生成 / 补缺摘要',action(()=>runJob(`/books/${encodeURIComponent(bookId)}/generate-digest`,{},()=>{if(token===state.route)route();})),'primary')
    ])),data.sourceUrl?el('div',{class:'podcast-provenance'},el('a',{href:data.sourceUrl,target:'_blank',rel:'noopener noreferrer'},'官方 Transcript ↗'),
      data.excerpted?el('span',{class:'count'},'摘要基于覆盖全文的分段摘录；正文为完整 Transcript。'):null):null,
    el('div',{class:'filters'},q,duplicateCount?el('label',{class:'duplicate-toggle'},showDuplicates,`显示重复文章（${duplicateCount}）`):null),tabs,el('div',{class:'filters'}),grid);
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
  progressStatus(reader,'正在保存…');
  return api(baseFor(reader)+'/progress',{method:'POST',data:position,keepalive}).then(()=>{
    if(key===JSON.stringify(currentPosition(reader)))progressStatus(reader,'已同步');
  }).catch(()=>{reader.lastPosition=null;progressStatus(reader,'待同步');if(!keepalive)toast('阅读进度暂未保存，网络恢复后会重试');});
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
function selectionBar(reader) {
  if($('#sheet').open)return;
  const selection=selectedQuote(),bar=$('#selection-tools');
  if(!selection){bar.hidden=true;state.selection=null;reader.selectionRange=null;return;}
  state.selection=selection;
  reader.selectionRange=window.getSelection().getRangeAt(0).cloneRange();
  bar.hidden=false;
  $('span',bar).textContent=selection.quote;
  positionSelectionBar(reader);
}
function positionSelectionBar(reader) {
  const bar=$('#selection-tools');if(!bar||bar.hidden||!reader.selectionRange||$('#sheet').open)return;
  const viewport=window.visualViewport;
  const left=viewport?.offsetLeft||0,top=viewport?.offsetTop||0;
  const width=viewport?.width||innerWidth,height=viewport?.height||innerHeight,margin=10;
  const toolbarBottom=$('.reader-toolbar')?.getBoundingClientRect().bottom||top;
  const rects=[...reader.selectionRange.getClientRects()].filter(r=>r.width&&r.height&&r.bottom>Math.max(top,toolbarBottom)&&r.top<top+height);
  if(!rects.length){bar.hidden=true;return;}
  const selection=window.getSelection(),range=reader.selectionRange;
  const forward=selection?.anchorNode===range.startContainer&&selection?.anchorOffset===range.startOffset;
  const anchor=forward?rects.at(-1):rects[0];
  bar.style.maxWidth=Math.max(0,width-margin*2)+'px';
  const size=bar.getBoundingClientRect();
  let y=anchor.bottom+9;
  if(y+size.height>top+height-margin)y=anchor.top-size.height-9;
  const x=Math.max(left+margin,Math.min(anchor.left+anchor.width/2-size.width/2,left+width-size.width-margin));
  bar.style.left=x+'px';bar.style.top=Math.max(top+margin,Math.min(y,top+height-size.height-margin))+'px';
}
async function readerPage(bookId,index,token,startParagraph=null) {
  const data=await api(`/books/${encodeURIComponent(bookId)}/articles/${index}`);if(token!==state.route)return;
  const reader={...data,bookId,index,ready:false,vocab:null,fontSize:Number(localStorage.getItem('kandu-font')||21)};state.reader=reader;
  reader.paragraphOffsets=[];reader.totalCharacters=0;
  for(const paragraph of data.article.paragraphs){reader.paragraphOffsets.push(reader.totalCharacters);reader.totalCharacters+=paragraph.length;}
  const article=el('article',{class:'reader'});
  const progress=el('div',{class:'reader-position'},el('div',{class:'reader-position-labels'},
    el('strong',{id:'reader-percentage'},'阅读进度 0%'),el('span',{id:'reader-paragraph'},`第 1 / ${data.article.paragraphs.length} 段`),
    el('span',{id:'reader-word-count'},`共 ${data.wordCount.toLocaleString()} 词`),
    el('span',{class:'reader-save-state',id:'reader-save-state'},'自动同步')),
    el('progress',{id:'reader-progress-bar',max:1,value:0,'aria-label':'文章阅读进度'}));
  const vocabButton=button('生成生词表',action(()=>openVocab(null)),'primary');vocabButton.id='vocab-button';
  const find=el('input',{type:'search',placeholder:'在本文中查找…','aria-label':'查找原文'});
  const findRow=el('div',{class:'find-row',hidden:true},find,el('span',{class:'count',id:'find-count'}),button('×',()=>{find.value='';findArticle(reader,'');findRow.hidden=true;}));
  const tools=el('div',{class:'reader-toolbar'},el('a',{href:`#/book/${encodeURIComponent(bookId)}`},'← 目录'),vocabButton,
    button(data.state==='read'?'已读 ✓':'标记已读',action(async event=>{
      const target=event.target.closest('button');target.disabled=true;
      try{await api(`/books/${encodeURIComponent(bookId)}/state`,{method:'POST',data:{articleIndex:index,state:'read'}});
        reader.state='read';target.textContent='已读 ✓';toast('已计入全局已读统计');}finally{target.disabled=false;}
    })),
    button('与 AI 讨论',action(()=>discussionSheet(reader))),button('查找',()=>{findRow.hidden=!findRow.hidden;if(!findRow.hidden)find.focus();}),
    el('span',{class:'grow'}),button('A−',()=>resizeReader(reader,-1)),button('A+',()=>resizeReader(reader,1)),
    el('a',{href:`/api/books/${encodeURIComponent(bookId)}/file`,target:'_blank',rel:'noopener'},data.ext==='pdf'?'原版 PDF ↗':'下载 EPUB ↗'),progress);
  const selection=el('div',{class:'selection-tools',id:'selection-tools',hidden:true,role:'toolbar','aria-label':'选中文字操作',
    onpointerdown:event=>event.preventDefault()},el('span',{}),
    button('查词并收藏',action(()=>lookupSelection(reader))),button('句子讲解',action(()=>analyzeSelection(reader))),
    button('引用提问',action(()=>discussionSheet(reader,state.selection))),button('×',()=>{selection.hidden=true;reader.selectionRange=null;}));
  const body=el('div',{class:'reader-body',id:'reader-body'});
  body.style.setProperty('--reading-size',reader.fontSize+'px');
  data.article.paragraphs.forEach((text,i)=>{
    const badge=button('本段生词 · 读取中',action(()=>openVocab(i)),'para-badge');badge.id='badge-'+i;
    const paragraph=el('section',{class:'paragraph',id:'para-'+i,'data-index':i},badge,el('p',{},text));
    paragraph.addEventListener('dblclick',action(async event=>{
      if(event.target.closest('button'))return;
      let chosen=selectedQuote();
      if(chosen) {
        const start=text.indexOf(chosen.quote);
        const sentences=text.match(/[^.!?]+[.!?]+["'’”]?|[^.!?]+$/g)||[text];let cursor=0;
        for(const sentence of sentences){if(start>=cursor&&start<cursor+sentence.length){chosen={quote:sentence.trim(),paragraph:i};break;}cursor+=sentence.length;}
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
    data.summaryZh?el('details',{class:'reader-summary'},el('summary',{},data.excerpted?'整期摘要 · 全文分段摘录':'文章摘要'),el('p',{},data.summaryZh)):null),selection,body);
  view.replaceChildren(article);window.scrollTo(0,0);
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
  updateReaderProgress(reader);
  readerScroll=()=>{
    if(state.reader!==reader)return;
    if(!progressFrame)progressFrame=requestAnimationFrame(()=>{progressFrame=null;updateReaderProgress(reader);positionSelectionBar(reader);});
    progressStatus(reader,'阅读中');clearTimeout(progressTimer);progressTimer=setTimeout(()=>saveProgress(),850);
  };
  window.addEventListener('scroll',readerScroll,{passive:true});
  // Record this article in recent reading even if the user stays at its beginning.
  await saveProgress();
  await loadVocab(reader);
}
function resizeReader(reader,delta){reader.fontSize=Math.max(16,Math.min(30,reader.fontSize+delta));$('#reader-body').style.setProperty('--reading-size',reader.fontSize+'px');localStorage.setItem('kandu-font',reader.fontSize);updateReaderProgress(reader);}
function findArticle(reader,query) {
  let count=0;const needle=query.toLocaleLowerCase();
  reader.article.paragraphs.forEach((text,i)=>{
    const p=$('#para-'+i+' p');p.replaceChildren();
    if(!needle){p.textContent=text;return;}
    const lower=text.toLocaleLowerCase();let cursor=0,index=lower.indexOf(needle);
    while(index>=0){p.append(text.slice(cursor,index),el('mark',{},text.slice(index,index+needle.length)));cursor=index+needle.length;count++;index=lower.indexOf(needle,cursor);}
    p.append(text.slice(cursor));
  });$('#find-count').textContent=query?`${count} 处匹配`:'';
}
async function loadVocab(reader) {
  const response=await api(baseFor(reader)+'/vocab');if(state.reader!==reader)return;
  reader.vocab=response;
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
    onclick:()=>{if($('#sheet').open)$('#sheet').close();}},title+' ↗'):el('span',{class:'word-source-link'},title);
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
    await runJob(baseFor(reader)+'/generate-vocab',{},async job=>{if(job.state==='done'&&state.reader===reader){await loadVocab(reader);await vocabSheet(reader,null);}});return;
  }
  await vocabSheet(reader,paragraph);
}
async function vocabSheet(reader,paragraph) {
  if(state.reader!==reader)return;
  const data=reader.vocab,body=el('div');
  const missing=paragraph===null?data.missing.length:data.missing.includes(paragraph);
  if(missing)body.append(el('p',{class:'notice'},paragraph===null?`全文还有 ${data.missing.length} 段待生成，当前只展示已有词条和个人收藏。`:'本段生词尚未生成，当前只展示已有词条和个人收藏。'));
  const actions=el('div',{class:'row'},button('生成 / 补缺全文生词',action(()=>runJob(baseFor(reader)+'/generate-vocab',{},async job=>{
    if(job.state==='done'&&state.reader===reader){await loadVocab(reader);vocabSheet(reader,paragraph);}
  })),'primary'),button('补充短语解释',action(()=>runJob(baseFor(reader)+'/generate-vocab',{phrasesOnly:true,paragraph},async job=>{
    if(job.state==='done'&&state.reader===reader){await loadVocab(reader);vocabSheet(reader,paragraph);}
  }))));body.append(actions);
  const search=el('input',{type:'search',class:'search',placeholder:'查找词条…','aria-label':'查找词条'}),list=el('div');
  body.append(el('div',{class:'filters'},search),list);
  function render(){
    list.replaceChildren();let total=0;
    const groups=[...(paragraph===null?[{index:-1,entries:data.vocab.titleEntries}]:[]),...data.vocab.paragraphs.filter(p=>paragraph===null||p.index===paragraph)];
    for(const group of groups){const entries=group.entries.filter(e=>e.word.toLowerCase().includes(search.value.toLowerCase()));if(!entries.length)continue;
      list.append(el('div',{class:'vocab-section'},group.index===-1?'标题生词':`第 ${group.index+1} 段 · ${entries.length} 项`));
      for(const entry of entries){list.append(entryNode(entry,()=>loadVocab(reader),{bookId:reader.bookId,index:reader.index,paragraph:group.index}));total++;}
    }
    if(paragraph===null&&data.vocab.idioms?.length){list.append(el('div',{class:'vocab-section'},'熟词僻义'));for(const idiom of data.vocab.idioms)if(idiom.word.toLowerCase().includes(search.value.toLowerCase())){list.append(entryNode({...idiom,sense:idiom.inText,root:'常见义 · '+idiom.common},null,{bookId:reader.bookId,index:reader.index,paragraph:null}));total++;}}
    if(!total)list.append(empty('当前没有可显示的词条',missing?'点击上方按钮生成词表。':'可以补充短语解释，或选中原文查词。'));
  }search.oninput=render;render();showSheet(paragraph===null?'本篇生词与短语':`第 ${paragraph+1} 段 · 生词与短语`,body);
}
async function lookupSelection(reader) {
  const chosen={...state.selection};if(!chosen.quote)return;
  const output=el('div',{},el('p',{class:'muted'},'正在查词…'));
  const sheet=showSheet('查词 · 本文含义',output);
  $('#selection-tools').hidden=true;
  const request={word:chosen.quote,paragraph:chosen.paragraph,start:chosen.start};
  let response;
  try{response=await api(baseFor(reader)+'/lookup',{method:'POST',data:request});}
  catch(error){if(sheet===state.sheet)output.replaceChildren(el('p',{class:'error'},error.message));return;}
  if(sheet!==state.sheet)return;
  const context=el('section',{class:'lookup-context','aria-live':'polite'});
  const details=el('details',{class:'lookup-dictionary'},el('summary',{},'词典 / 个人释义 · 已加入个人生词本'),
    entryNode({...response.saved,inWordbook:true,senseSource:'wordbook'}));
  output.replaceChildren(el('div',{class:'lookup-word'},el('strong',{},chosen.quote),el('span',{class:'phonetic'},response.saved.phonetic||'')),
    context,details,sourceNode({...response.saved.source,contextMeaning:null}));
  const refreshVocab=()=>{if(state.reader===reader)return loadVocab(reader);};
  const explain=async()=>{
    context.replaceChildren(el('p',{class:'muted'},'AI 正在结合当前句子与段落解释…'));
    try {
      await runJob(baseFor(reader)+'/lookup-context',{...request,savedWord:response.saved.word},async job=>{
        if(sheet!==state.sheet)return;
        if(job.state==='done'){context.replaceChildren(contextMeaningNode(job.result.meaning));await refreshVocab();}
        else showFailure(job.error||'本文解释未完成');
      },job=>{if(sheet===state.sheet&&job.state!=='done'&&$('p.muted',context))$('p.muted',context).textContent=job.status;});
    }catch(error){if(sheet===state.sheet)showFailure(error.message);}
  };
  const showFailure=message=>{details.open=true;context.replaceChildren(el('p',{class:'muted'},'本文含义暂未生成：'+message),button('重试本文解释',action(explain)));};
  if(response.contextMeaning)context.append(contextMeaningNode(response.contextMeaning));
  else if(response.canExplain)await explain();
  else {details.open=true;context.append(el('p',{class:'muted'},'配置 LLM 后，可结合本文语境解释词义。'),el('a',{href:'#/settings'},'前往设置 →'));}
  await refreshVocab();
}
function contextMeaningNode(meaning,compact=false) {
  return el('section',{class:'context-meaning'+(compact?' compact':'')},
    el('div',{class:'context-meaning-heading'},el('span',{},'AI · 本文含义'),meaning.pos?el('small',{},meaning.pos):null),
    el('p',{class:'context-sense'},meaning.sense),el('p',{class:'context-explanation'},meaning.explanation),
    !compact&&meaning.sentence?el('p',{class:'quote'},meaning.sentence):null,
    !compact&&meaning.translation?el('p',{class:'context-translation'},meaning.translation):null);
}
async function analyzeSelection(reader) {
  const chosen={...state.selection};if(!chosen.quote)return;
  const output=el('div',{},el('p',{class:'quote'},chosen.quote),el('p',{class:'muted'},'正在准备句子讲解…'));
  const sheet=showSheet('句子讲解',output);
  await runJob(baseFor(reader)+'/analyze',{sentence:chosen.quote,paragraph:chosen.paragraph},job=>{
    if(sheet!==state.sheet)return;
    if(job.state!=='done'){output.append(el('p',{class:'error'},job.error||'未能完成讲解'));return;}
    renderAnalysis(output,job.result,chosen.quote,{bookId:reader.bookId,index:reader.index,paragraph:chosen.paragraph});
  },job=>{if(sheet===state.sheet&&job.state!=='done'){$('p.muted',output).textContent=job.status;}});
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
    try {
      jobId=await runJob(baseFor(reader)+'/discussion',{question,quote:selection?.quote||'',paragraph:selection?.paragraph},async job=>{
        stop.hidden=true;submit.disabled=false;
        if(job.state==='error'){$('.answer-text',answer).textContent=job.text||job.error;status.textContent='回答未完成，可重新提问';}
        else status.textContent=job.state==='stopped'?'已停止，已收到的内容已保存':'会话已保存';
      },job=>{
        if(sheet!==state.sheet)return;
        status.textContent=job.status;
        if(job.text){$('.answer-text',answer).textContent=job.text;messages.scrollTop=messages.scrollHeight;}
      });
      input.value='';stop.hidden=false;
    }catch(error){user.remove();answer.remove();submit.disabled=false;throw error;}
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
    if(word)data.original=word;await api('/wordbook',{method:'POST',data});$('#sheet').close();toast('词条已保存');
    if(state.reader)await loadVocab(state.reader);else if(location.hash==='#/wordbook')route();});
  showSheet(word?'编辑个人词条':'添加个人词条',form);
}

async function settingsPage(token) {
  const [data,sync]=await Promise.all([api('/settings'),api('/reading-sync')]);if(token!==state.route)return;
  const level=el('select',{},...Object.entries({cet4:'四级 CET-4',cet6:'六级 CET-6',toefl:'TOEFL',gre:'GRE'}).map(([k,v])=>el('option',{value:k},v)));level.value=data.vocabLevel;
  level.onchange=action(async()=>{await api('/settings',{method:'POST',data:{vocabLevel:level.value}});toast('词汇水平已保存');});
  const models=el('div');
  for(const item of data.models)models.append(el('div',{class:'model-item'},el('div',{},item.name,el('small',{},item.provider+' · '+item.model)),
    el('div',{class:'model-actions'},button('编辑',()=>editModel(item)),
      button(item.name===data.defaultModel?'默认模型':'设为默认',action(async()=>{await api('/settings',{method:'POST',data:{defaultModel:item.name}});route();}),item.name===data.defaultModel?'secondary':''))));
  if(!data.models.length)models.append(el('p',{class:'muted'},'尚未配置模型。已有的阅读结果仍可浏览。'));
  const name=el('input',{placeholder:'配置名称（可留空）'}),json=el('textarea',{placeholder:'粘贴完整 cc-switch / Claude Code / OpenAI 配置 JSON',rows:7,spellcheck:false,autocomplete:'off'});
  const form=el('form',{class:'form-stack'},el('label',{},'名称',name),el('label',{},'配置 JSON',json),
    el('small',{class:'muted'},'密钥只保存在服务器，不会回传到页面。'),el('button',{type:'submit',class:'primary'},'保存并设为默认'));
  form.onsubmit=action(async event=>{event.preventDefault();await api('/settings',{method:'POST',data:{name:name.value,configJson:json.value}});json.value='';toast('模型配置已保存');route();});
  view.replaceChildren(heading('MAKE IT YOURS','阅读设置','模型和词汇水平由这台服务器统一管理'),
    el('div',{class:'settings-grid'},el('section',{class:'panel'},el('h2',{},'阅读偏好'),el('label',{},'默认已掌握的词汇',level),el('hr'),el('h2',{},'模型配置'),models),
      el('section',{class:'panel'},el('h2',{},'导入模型配置'),form)),syncSettingsPanel(sync));
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
  const data=await api('/reading-stats');if(token!==state.route)return;
  const search=el('input',{type:'search',class:'search',placeholder:'搜索标题、刊物或网站…','aria-label':'搜索已读文章'});
  const list=el('div',{class:'completed-list'}),count=el('span',{class:'count'});
  const render=()=>{
    const query=search.value.trim().toLocaleLowerCase();
    const found=data.articles.filter(a=>[a.title,...a.sources.map(s=>s.source)].join(' ').toLocaleLowerCase().includes(query));
    count.textContent=`${found.length} 篇`;
    list.replaceChildren(...found.map(a=>el('article',{class:'completed-card'},
      el('div',{class:'completed-heading'},el('h2',{},a.title),el('strong',{},a.wordCount?`${a.wordCount.toLocaleString()} 词`:'词数待补全')),
      el('div',{class:'completed-meta'},el('time',{datetime:new Date(a.readAt).toISOString()},new Date(a.readAt).toLocaleDateString()),
        el('span',{},a.sources.length>1?`${a.sources.length} 个出处 · 标题去重后计为 1 篇`:'已读文章')),
      el('div',{class:'completed-sources'},...a.sources.map(s=>el('a',s.kind==='chrome'?{href:s.url,target:'_blank',rel:'noopener noreferrer'}:
        {href:linkFor(s.bookId,s.articleIndex)},`${s.kind==='chrome'?'网页':'刊物'} · ${s.source}${s.kind==='chrome'?' ↗':' →'}`))))));
    if(!found.length)list.append(empty(query?'没有匹配的已读文章':'你的阅读积累，从这里开始',
      '读到文章结尾或标记为已读，即可加入；Chrome 网页文章也可以同步到这里。'));
  };
  search.oninput=render;
  view.replaceChildren(heading('YOUR READING JOURNEY','已读统计','每读完一篇，都是一次积累。跨刊物、跨网页，记录你的阅读足迹。'),
    el('div',{class:'reading-totals'},el('div',{},el('strong',{'data-stat':'articles'},data.articleCount.toLocaleString()),el('span',{},'已读文章 · 按标题去重')),
      el('div',{},el('strong',{'data-stat':'words'},data.totalWords.toLocaleString()),el('span',{},'累计阅读词数'))),
    el('p',{class:'count'},'标题忽略大小写、空白和标点差异；同标题各出处合并，词数取最大值，不重复累加。'),
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
    el('div',{class:'row'},submit,button('取消',()=>$('#sheet').close())));
  form.onsubmit=async event=>{
    event.preventDefault();submit.disabled=true;error.hidden=true;
    try {
      await api('/settings',{method:'POST',data:{originalName:initial.name,modelConfig:{name:name.value,
        provider:provider.value,baseUrl:baseUrl.value,model:model.value,apiKey:key.value}}});
      key.value='';$('#sheet').close();toast('模型配置已更新');await route();
    } catch(failure) {error.textContent=failure.message;error.hidden=false;}
    finally {submit.disabled=false;}
  };
  showSheet('编辑模型配置',form);
}

async function route() {
  if(!state.csrf){showLogin();return;}
  if(state.reader){await saveProgress(true);document.removeEventListener('selectionchange',state.reader.selectionHandler);
    window.removeEventListener('resize',state.reader.selectionResize);
    window.visualViewport?.removeEventListener('resize',state.reader.selectionResize);
    window.visualViewport?.removeEventListener('scroll',state.reader.selectionResize);}
  clearTimeout(progressTimer);cancelAnimationFrame(progressFrame);progressFrame=null;if(readerScroll)window.removeEventListener('scroll',readerScroll);readerScroll=null;
  state.reader=null;state.selection=null;
  if($('#sheet').open)$('#sheet').close();
  const token=++state.route;$('#navigation').hidden=false;
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
  }catch(error){if(token===state.route&&state.csrf)view.replaceChildren(empty('暂时无法打开',error.message),button('重试',()=>route(),'primary'));}
}
window.addEventListener('hashchange',route);
window.addEventListener('pagehide',()=>saveProgress(true));
document.addEventListener('visibilitychange',()=>{if(document.hidden)saveProgress(true);});
(async()=>{try{const session=await api('/session');if(session.authenticated){state.csrf=session.csrf;await route();}else showLogin();}catch(error){view.replaceChildren(empty('连接服务器失败',error.message));}})();
