'use strict';
window.KanduPWA=(()=>{
  let registration=null,installPrompt=null,updating=false,reloadRequested=false,lastCheck=0,banner=null,dismissedWorker=null;
  const listeners=new Set();
  const standalone=()=>matchMedia('(display-mode:standalone)').matches||navigator.standalone===true;
  const node=(tag,attrs={},...children)=>{
    const element=document.createElement(tag);
    for(const [key,value]of Object.entries(attrs)){
      if(key==='onclick')element.addEventListener('click',value);
      else element.setAttribute(key,value);
    }
    element.append(...children);return element;
  };
  const button=(label,onclick,cls='')=>node('button',{type:'button',class:cls,onclick},label);
  function notify(){for(const listener of listeners)listener();}
  function removeBanner(){banner?.remove();banner=null;}
  function showUpdate(){
    if(!registration?.waiting||registration.waiting===dismissedWorker)return;
    removeBanner();
    const status=node('span',{},'刊读有新版本，阅读记录保存后即可更新。');
    const update=button('更新',()=>activate(status,update),'primary');
    banner=node('aside',{class:'pwa-update',role:'status','aria-label':'应用更新'},status,
      node('div',{class:'row'},update,button('稍后',()=>{dismissedWorker=registration.waiting;removeBanner();})));
    document.body.append(banner);
  }
  async function activate(status,control){
    if(updating)return;
    updating=true;control.disabled=true;status.textContent='正在保存阅读记录…';
    try{
      await window.preparePwaReload?.();
      if(navigator.onLine===false)throw new Error('请连接网络后更新，当前阅读内容已保留。');
      reloadRequested=true;
      if(registration?.waiting)registration.waiting.postMessage({type:'KANDU_ACTIVATE_UPDATE'});
      else location.reload();
    }catch(error){status.textContent=error.message||'暂时无法更新，请稍后重试。';}
    finally{updating=false;control.disabled=false;}
  }
  async function check(){
    if(!registration||document.hidden||navigator.onLine===false||Date.now()-lastCheck<60_000)return;
    lastCheck=Date.now();try{await registration.update();}catch{}
  }
  async function install(status){
    if(!installPrompt){status.textContent='iPhone：点击浏览器的分享按钮 → 添加到主屏幕 → 添加；如有「作为 Web App 打开」，请开启。Chrome 中未找到入口时，可用 Safari 打开相同地址添加。';return;}
    const prompt=installPrompt;installPrompt=null;notify();
    try{await prompt.prompt();await prompt.userChoice;}catch{}
    notify();
  }
  function panel(){
    const status=node('p',{class:'muted'}),actions=node('div',{class:'row'});
    const section=node('section',{class:'panel pwa-settings'},node('h2',{},'安装到主屏幕'),status,actions,
      node('p',{class:'count'},'主屏幕入口使用同一书库和阅读记录。首次打开请联网登录；启动界面会缓存在本机，已打开文章继续使用阅读缓存。'));
    const render=()=>{
      status.textContent=standalone()?'刊读正在独立窗口中运行。':
        '在 iPhone 分享菜单中选择「添加到主屏幕」，以后可直接点击刊读图标阅读。';
      actions.replaceChildren();
      if(!standalone())actions.append(button(installPrompt?'安装刊读':'查看安装步骤',()=>install(status),'primary'));
      if(registration?.waiting)actions.append(button('更新刊读',event=>activate(status,event.currentTarget)));
      if(!window.isSecureContext)status.textContent='请使用 HTTPS 域名访问后添加到主屏幕；当前局域网 HTTP 地址不能启用完整启动缓存。';
    };
    render();listeners.add(render);section.cleanup=()=>listeners.delete(render);
    return section;
  }
  window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();installPrompt=event;notify();});
  window.addEventListener('appinstalled',()=>{installPrompt=null;notify();});
  window.addEventListener('online',check);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)check();});
  if('serviceWorker' in navigator&&window.isSecureContext){
    navigator.serviceWorker.addEventListener('controllerchange',()=>{
      if(reloadRequested)location.reload();
      notify();
    });
    window.addEventListener('load',async()=>{
      try{
        registration=await navigator.serviceWorker.register('/sw.js',{scope:'/',updateViaCache:'none'});
        const watch=()=>{
          const worker=registration.installing;
          worker?.addEventListener('statechange',()=>{
            if(worker.state==='installed'&&navigator.serviceWorker.controller){showUpdate();notify();}
          });
        };
        registration.addEventListener('updatefound',watch);watch();showUpdate();notify();
      }catch{} // Normal browser reading still works if storage/registration fails.
    },{once:true});
  }
  setInterval(check,3600_000);
  return {panel};
})();
