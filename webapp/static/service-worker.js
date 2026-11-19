'use strict';
const BUILD=__KANDU_BUILD__;
const ASSETS=__KANDU_ASSETS__;
const PREFIX='kandu-public-shell-';
const CACHE=PREFIX+BUILD;
const assetURLs=new Set(ASSETS.map(path=>new URL(path,self.location.origin).href));

self.addEventListener('install',event=>event.waitUntil((async()=>{
  const cache=await caches.open(CACHE);
  try{
    // Fetch the public shell without cookies. Version checks prevent an update
    // deployed halfway through installation from producing a mixed bundle.
    for(const path of ['/',...ASSETS]){
      const response=await fetch(path,{cache:'reload',credentials:'omit'});
      const expected=path==='/'?BUILD:new URL(path,self.location.origin).searchParams.get('v');
      const actual=response.headers.get(path==='/'?'X-Kandu-Shell-Version':'X-Kandu-Asset-Version');
      if(!response.ok||response.redirected||actual!==expected)throw new Error('Incomplete application shell');
      await cache.put(path,response);
    }
  }catch(error){await caches.delete(CACHE);throw error;}
  // Updated workers wait for explicit user activation. A first install activates
  // normally; an open article is never reloaded just because files changed.
})()));

self.addEventListener('activate',event=>event.waitUntil((async()=>{
  for(const name of await caches.keys())if(name.startsWith(PREFIX)&&name!==CACHE)await caches.delete(name);
  await self.clients.claim();
})()));

self.addEventListener('message',event=>{
  if(event.data?.type==='KANDU_ACTIVATE_UPDATE')self.skipWaiting();
});

async function navigation(request){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),3500);
  try{
    const response=await fetch(request,{signal:controller.signal});
    if(response.ok)return response;
    const saved=await caches.match('/',{cacheName:CACHE});
    return saved||response;
  }catch(error){
    const saved=await caches.match('/',{cacheName:CACHE});
    if(saved)return saved;
    throw error;
  }finally{clearTimeout(timer);}
}

self.addEventListener('fetch',event=>{
  const request=event.request,url=new URL(request.url);
  if(request.method!=='GET'||url.origin!==self.location.origin)return;
  if(request.mode==='navigate'&&url.pathname==='/'){
    event.respondWith(navigation(request));return;
  }
  if(assetURLs.has(url.href))event.respondWith((async()=>{
    const saved=await caches.match(request,{cacheName:CACHE});
    return saved||fetch(request);
  })());
  // APIs, uploads, books, covers, sessions and model configuration are never
  // intercepted. Existing authenticated IndexedDB caching owns reading data.
});
