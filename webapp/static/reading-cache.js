'use strict';
// Metadata is separate: touching a position or checking the budget must not
// deserialize 50 MB of cached text. No auth/configuration values enter this DB.
window.ReadingCache=(()=>{
  const LIMIT=50*1024*1024,ARTICLES=20,DB='kandu-reading-v1';
  let db=null,scope='',generation=0,order=0,serial=Promise.resolve();
  const accessed=()=>order=Math.max(Date.now(),order+.01);
  const memory=new Map(),clone=value=>structuredClone(value);
  function transaction(mode,callback){
    if(!db)return Promise.resolve();
    return new Promise((resolve,reject)=>{
      const tx=db.transaction(['entries','metadata'],mode);let result;
      tx.oncomplete=()=>resolve(result);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);
      callback(tx.objectStore('entries'),tx.objectStore('metadata'),value=>{result=value;});
    });
  }
  function enqueue(callback){serial=serial.catch(()=>{}).then(callback);return serial;}
  function victims(rows){
    const groups=new Map();for(const row of rows)if(row.group)groups.set(row.group,Math.max(groups.get(row.group)||0,row.used));
    const keep=new Set([...groups].sort((a,b)=>b[1]-a[1]).slice(0,ARTICLES).map(([key])=>key));
    const removed=new Set();let bytes=rows.reduce((n,row)=>n+row.bytes,0);
    for(const row of rows.filter(row=>row.group&&!keep.has(row.group))){removed.add(row.key);bytes-=row.bytes;}
    for(const row of [...rows].sort((a,b)=>a.used-b.used)){
      if(bytes<=LIMIT)break;
      for(const victim of row.group?rows.filter(r=>r.group===row.group):[row]){
        if(!removed.has(victim.key)){removed.add(victim.key);bytes-=victim.bytes;}
      }
    }
    return removed;
  }
  async function init(value){
    scope=value;generation++;memory.clear();await serial.catch(()=>{});db?.close();
    try{
      db=await new Promise((resolve,reject)=>{
        const request=indexedDB.open(DB,2);
        request.onupgradeneeded=()=>{
          const connection=request.result;
          if(!connection.objectStoreNames.contains('entries'))connection.createObjectStore('entries',{keyPath:'key'});
          if(!connection.objectStoreNames.contains('metadata')){
            connection.createObjectStore('metadata',{keyPath:'key'});request.transaction.objectStore('entries').clear();
          }
        };
        request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
        request.onblocked=()=>reject(new Error('Cache database upgrade blocked'));
      });
      db.onversionchange=()=>{db.close();db=null;};
      await transaction('readwrite',(values,metadata)=>{const req=metadata.openCursor();req.onsuccess=()=>{
        const cursor=req.result;if(!cursor)return;
        if(cursor.value.scope!==scope){values.delete(cursor.key);cursor.delete();}cursor.continue();};});
    }catch{db=null;}
  }
  async function get(key){
    if(!scope)return null;
    const epoch=generation;let record;
    if(db)try{record=await transaction('readonly',(values,metadata,done)=>{
      const value=values.get(key),meta=metadata.get(key);
      const finish=()=>{if(meta.readyState==='done'&&value.readyState==='done'&&meta.result&&value.result)
        done({...meta.result,value:value.result.value});};
      value.onsuccess=finish;meta.onsuccess=finish;
    });}catch{}
    else record=memory.get(key);
    if(epoch!==generation||!record||record.scope!==scope||record.value===undefined)return null;
    record.used=accessed();
    if(!db){memory.set(key,record);return clone(record);}
    const used=record.used;
    enqueue(async()=>{if(epoch!==generation)return;
      try{await transaction('readwrite',(_,metadata)=>{const req=metadata.get(key);req.onsuccess=()=>{
        if(req.result?.scope===scope)metadata.put({...req.result,used});};});}catch{}});
    return clone(record);
  }
  function put(key,value,{group='',checked=Date.now(),etag=''}={}){
    const epoch=generation,owner=scope;
    const meta={key,scope:owner,group,checked,etag,used:accessed(),bytes:new TextEncoder().encode(JSON.stringify(value)).length};
    if(!owner||meta.bytes>LIMIT)return Promise.resolve();
    const snapshot=clone(value);
    return enqueue(async()=>{
      if(epoch!==generation)return;
      if(db)try{
        await transaction('readwrite',(values,metadata)=>{
          const req=metadata.getAll();req.onsuccess=()=>{
            const rows=req.result.filter(row=>row.key!==key&&row.scope===owner);rows.push(meta);
            const removed=victims(rows);for(const victim of removed){values.delete(victim);metadata.delete(victim);}
            if(!removed.has(key)){values.put({key,value:snapshot});metadata.put(meta);}
          };
        });return;
      }catch{db.close();db=null;}
      memory.set(key,{...meta,value:snapshot});for(const victim of victims([...memory.values()]))memory.delete(victim);
    });
  }
  function remove(match){
    const epoch=generation;
    return enqueue(async()=>{if(epoch!==generation)return;
      for(const key of memory.keys())if(match(key))memory.delete(key);
      try{await transaction('readwrite',(values,metadata)=>{const req=metadata.openCursor();req.onsuccess=()=>{
        const cursor=req.result;if(!cursor)return;if(match(cursor.key)){values.delete(cursor.key);cursor.delete();}cursor.continue();};});}catch{}
    });
  }
  function clear(){generation++;return enqueue(async()=>{memory.clear();
    try{await transaction('readwrite',(values,metadata)=>{values.clear();metadata.clear();});}catch{}
  });}
  function stale(match=()=>true){
    const epoch=generation;
    return enqueue(async()=>{if(epoch!==generation)return;
      for(const record of memory.values())if(match(record.key))record.checked=0;
      try{await transaction('readwrite',(_,metadata)=>{const req=metadata.openCursor();req.onsuccess=()=>{
        const cursor=req.result;if(!cursor)return;if(match(cursor.key))cursor.update({...cursor.value,checked:0});cursor.continue();};});}catch{}
    });
  }
  async function info(){
    await serial.catch(()=>{});let records=[...memory.values()];
    try{records=await transaction('readonly',(_,metadata,done)=>{const req=metadata.getAll();req.onsuccess=()=>done(req.result);})||records;}catch{}
    records=records.filter(row=>row.scope===scope);
    return {bytes:records.reduce((n,row)=>n+row.bytes,0),articles:new Set(records.filter(row=>row.group).map(row=>row.group)).size,
      limit:LIMIT,maxArticles:ARTICLES,persistent:!!db};
  }
  return {init,get,put,remove,clear,stale,info};
})();
