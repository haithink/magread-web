'use strict';

// Native SVG charts keep private statistics local, without a CDN or chart SDK.
function readingAnalyticsPanel(analytics) {
  const node=(tag,attrs={},...children)=>{
    const element=document.createElement(tag);
    for(const [k,v]of Object.entries(attrs))element.setAttribute(k,v);
    element.append(...children.flat());return element;
  };
  const svgNode=(tag,attrs={},text=null)=>{
    const element=document.createElementNS('http://www.w3.org/2000/svg',tag);
    for(const [k,v]of Object.entries(attrs))element.setAttribute(k,v);
    if(text!==null)element.textContent=text;return element;
  };
  const fmt=value=>Math.round(value).toLocaleString();
  const dayLabel=day=>day.slice(5).replace('-','/');
  const root=node('section',{class:'reading-analytics','aria-label':'阅读数据可视化'});
  const range=node('select',{'aria-label':'图表日期范围'},...[[7,'最近 7 天'],[30,'最近 30 天'],[90,'最近 90 天'],[365,'最近一年']].map(([value,label])=>node('option',{value},label)));
  range.value='30';
  const period=node('p',{class:'count',id:'reading-period-summary','aria-live':'polite'});
  root.append(node('div',{class:'analytics-section-heading'},node('div',{},node('span',{class:'eyebrow'},'YOUR DAILY RHYTHM'),node('h2',{},'阅读量与速度')),range),period);
  const chartGrid=node('div',{class:'reading-chart-grid'});
  const drawCallbacks=[];

  function chart(title,subtitle,keys,unit) {
    const svg=svgNode('svg',{role:'img','aria-label':title,class:'reading-chart'});
    const detail=node('p',{class:'reading-chart-detail','aria-live':'polite'});
    const slider=node('input',{type:'range',min:0,value:0,step:1,class:'chart-day-slider','aria-label':title+'，选择日期'});
    const legends=node('div',{class:'chart-legends'},...keys.map(k=>node('span',{class:'legend-'+k.key},k.label)));
    const compare=keys.length>1?node('input',{type:'checkbox','aria-label':'对比每日读完文章词数'}):null;
    const panel=node('section',{class:'reading-chart-panel'},node('h3',{},title),node('p',{class:'count'},subtitle),legends,svg,slider,detail);
    if(compare)panel.insertBefore(node('label',{class:'chart-compare'},compare,'对比读完文章词数'),legends);
    chartGrid.append(panel);
    let series=[],geometry=null,marker=null;
    const show=index=>{
      if(!series.length)return;
      index=Math.max(0,Math.min(series.length-1,index));slider.value=String(index);
      const row=series[index];
      detail.textContent=row.date+' · '+keys.filter((k,i)=>i===0||compare?.checked).map(k=>`${k.label} ${row[k.key]===null?'样本不足':fmt(row[k.key])+' '+unit}`).join(' · ');
      slider.setAttribute('aria-valuetext',detail.textContent);
      if(marker&&geometry){const x=geometry.x(index);marker.setAttribute('x1',x);marker.setAttribute('x2',x);}
    };
    const draw=()=>{
      series=analytics.daily.slice(-Number(range.value));
      const activeKeys=keys.filter((k,i)=>i===0||compare?.checked);
      if(compare)legends.lastElementChild.hidden=!compare.checked;
      const width=Math.max(285,panel.clientWidth-44),height=235;
      const left=48,right=16,top=20,bottom=38,w=width-left-right,h=height-top-bottom;
      svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.replaceChildren();
      const values=series.flatMap(row=>activeKeys.map(k=>row[k.key])).filter(value=>value!==null);
      const max=Math.max(unit==='词/分'?100:100,...values);
      // Round the axis upward to an easily readable tick interval.
      const raw=max/4,magnitude=10**Math.floor(Math.log10(raw)),step=Math.ceil(raw/magnitude)*magnitude,yMax=step*4;
      const x=index=>left+index*w/Math.max(1,series.length-1),y=value=>top+h-value*h/yMax;
      geometry={x};
      svg.append(svgNode('text',{x:left,y:11,class:'chart-axis-unit'},unit));
      for(let i=0;i<=4;i++) {
        const yy=y(i*step),label=i*step;
        svg.append(svgNode('line',{x1:left,x2:width-right,y1:yy,y2:yy,class:'chart-gridline'}),
          svgNode('text',{x:left-9,y:yy+4,'text-anchor':'end',class:'chart-axis-label'},label>=10000?(label/1000).toLocaleString()+'k':fmt(label)));
      }
      const ticks=width<400?3:5;
      for(let i=0;i<ticks;i++) {
        const index=Math.round(i*(series.length-1)/(ticks-1));
        svg.append(svgNode('text',{x:x(index),y:height-12,'text-anchor':i===0?'start':i===ticks-1?'end':'middle',class:'chart-axis-label'},dayLabel(series[index].date)));
      }
      for(const key of activeKeys) {
        let path='',connected=false;
        series.forEach((row,index)=>{
          const value=row[key.key];
          if(value===null){connected=false;return;}
          path+=`${connected?'L':'M'}${x(index).toFixed(2)},${y(value).toFixed(2)} `;connected=true;
          if(key.key==='wordsPerMinute'||series.length<=30)svg.append(svgNode('circle',{cx:x(index),cy:y(value),r:2.5,class:'chart-dot series-'+key.key}));
        });
        svg.append(svgNode('path',{d:path,fill:'none',class:'chart-line series-'+key.key}));
      }
      marker=svgNode('line',{x1:left,x2:left,y1:top,y2:top+h,class:'chart-marker'});svg.append(marker);
      if(!values.some(v=>v>0))svg.append(svgNode('text',{x:left+w/2,y:top+h/2,'text-anchor':'middle',class:'chart-empty'},unit==='词/分'?'积累有效阅读样本后显示':'开始阅读后，在这里看见积累'));
      slider.max=String(series.length-1);show(series.length-1);
    };
    const point=event=>{
      const bounds=svg.getBoundingClientRect(),width=Number(svg.viewBox.baseVal.width);
      const local=(event.clientX-bounds.left)*width/bounds.width;
      show(Math.round((local-48)/(width-64)*(series.length-1)));
    };
    svg.addEventListener('pointermove',point);svg.addEventListener('pointerdown',point);
    slider.addEventListener('input',()=>show(Number(slider.value)));
    compare?.addEventListener('change',draw);
    drawCallbacks.push(draw);
    return panel;
  }
  chart('每日阅读词数','包含尚未读完的文章；可对比按标题去重、归到首次完成日的读完词数。',[
    {key:'measuredWords',label:'计时阅读'},{key:'completedWords',label:'读完文章'}],'词');
  chart('每日阅读速度','当天计时阅读词数 ÷ 有效分钟数；至少 30 秒、20 词才显示。空白表示样本不足。',[
    {key:'wordsPerMinute',label:'估算速度'}],'词/分');
  root.append(chartGrid);

  const extra=node('div',{class:'reading-insights-grid'});
  const calendar=node('section',{class:'reading-chart-panel reading-calendar'},node('h3',{},'阅读日历'),
    node('p',{class:'count'},`最近 12 周 · 最长连续 ${analytics.longestStreak} 天 · 颜色代表有效阅读时长`));
  const cells=node('div',{class:'reading-heatmap','aria-label':'最近 12 周阅读日历'});
  const calendarDetail=node('p',{class:'reading-chart-detail','aria-live':'polite'},'选择一天查看阅读时长和词数。');
  const lastDay=new Date(analytics.daily.at(-1).date+'T12:00:00Z');
  const calendarDays=analytics.daily.slice(-(77+(lastDay.getUTCDay()+6)%7+1));
  calendar.append(node('div',{class:'calendar-date-range'},calendarDays[0].date+' — '+calendarDays.at(-1).date));
  for(const row of calendarDays) {
    const minutes=row.activeMs/60000,level=minutes>=60?4:minutes>=30?3:minutes>=10?2:minutes>0?1:0;
    const text=`${row.date} · ${Math.round(minutes)} 分钟 · 计时阅读 ${fmt(row.measuredWords)} 词 · 读完 ${row.completedArticles} 篇`;
    const cell=node('button',{type:'button',class:'heatmap-cell heat-level-'+level,title:text,'aria-label':text,'data-date':row.date});
    const describe=()=>{calendarDetail.textContent=text;};
    cell.addEventListener('focus',describe);cell.addEventListener('pointerenter',describe);cell.addEventListener('click',describe);cells.append(cell);
  }
  const weekdays=node('div',{class:'heatmap-weekdays','aria-hidden':'true'},...['一','二','三','四','五','六','日'].map(day=>node('span',{},day)));
  calendar.append(node('div',{class:'heatmap-layout'},weekdays,cells),node('div',{class:'heatmap-legend'},'少',...[0,1,2,3,4].map(i=>node('span',{class:'heatmap-cell heat-level-'+i,'aria-hidden':'true'})),'多'),calendarDetail);
  const sources=node('section',{class:'reading-chart-panel reading-source-distribution'},node('h3',{},'阅读来源分布'),
    node('p',{class:'count'},'按已完成文章总词数统计；重复标题归入最早完成的出处，每篇只计一次。'));
  const rows=analytics.sources.slice(0,5);
  if(analytics.sources.length>5)rows.push({name:'其他',words:analytics.sources.slice(5).reduce((s,r)=>s+r.words,0),articleCount:analytics.sources.slice(5).reduce((s,r)=>s+r.articleCount,0)});
  const max=Math.max(1,...rows.map(r=>r.words));
  if(!rows.length)sources.append(node('p',{class:'muted'},'读完第一篇文章后显示来源分布。'));
  for(const row of rows)sources.append(node('div',{class:'reading-source-row'},node('div',{},node('strong',{},row.name),node('span',{},`${row.articleCount} 篇 · ${fmt(row.words)} 词`)),
    node('meter',{min:0,max,value:row.words,'aria-label':`${row.name}：${fmt(row.words)} 词`})));
  extra.append(calendar,sources);root.append(extra);
  root.append(node('details',{class:'reading-measurement-notes'},node('summary',{},'阅读量和速度如何计算？'),
    node('p',{},'累计实际阅读词数（估算）＝已完成文章全文词数＋未读完文章的计时阅读估算，与图表日期范围无关。历史已读文章和 Chrome 同步完成记录也按全文计入；完成后用全文词数替换此前部分阅读量，不再重复叠加。已完成同标题各出处取最大全文词数；未完成同标题各出处取最大的累计计时词数。提前标记已读也按全文计算。'),
    node('p',{},'有效时长只累计前台且获得焦点的阅读页面；生成当前文章生词/短语、切换标签页、弹出查词/讲解窗口、手动暂停或 90 秒无操作会暂停。'),
    node('p',{},'计时阅读词数根据正文位置前进估算，不代表逐字阅读或理解；快速跳转不计词数。同一次阅读往回翻不重复计词，重新打开文章从头复读则计入当天阅读量。文章和每日速度均为计时词数除以有效分钟数，跨文章平均按总时间加权。'),
    node('p',{},'每日曲线及阅读速度继续使用原始计时采样，和累计阅读量的统计口径不同。历史文章或提前标记已读所计入的全文词数不会补算为计时样本、有效时长或速度。多窗口或多设备同时打开时，有效时长不重复累计。日期按当前设备的 UTC 偏移划分；午夜前后的计时会拆分到两天。')));
  const update=()=>{
    const days=analytics.daily.slice(-Number(range.value));
    const words=days.reduce((s,d)=>s+d.measuredWords,0),ms=days.reduce((s,d)=>s+d.activeMs,0);
    period.textContent=`${days[0].date} — ${days.at(-1).date} · 计时阅读 ${fmt(words)} 词 · 有效阅读 ${Math.round(ms/60000)} 分钟 · ${days.filter(d=>d.activeMs>0).length} 个阅读日`;
    drawCallbacks.forEach(draw=>draw());
  };
  range.addEventListener('change',update);
  // Draw after insertion so axes use the actual phone/desktop panel width.
  const observer=new ResizeObserver(update);observer.observe(chartGrid);
  root.cleanup=()=>observer.disconnect();
  requestAnimationFrame(update);
  return root;
}
