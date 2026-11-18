function mbClipRun(APP){
/* Runs on the post you are looking at (from the bookmark or the toolbar button). Opens the save window on this
   app's address, sends it the page, then downloads the files it asks for (in the page, so the site's login applies)
   and passes them back. It only talks to that one window. Only block comments here: the bookmark is one line. */
const url=location.href;let bub=null;
const say=(t,ok,click)=>{if(!bub){bub=document.createElement('div');Object.assign(bub.style,{position:'fixed',right:'16px',bottom:'16px',zIndex:'2147483647',padding:'10px 16px',borderRadius:'999px',font:'600 14px system-ui,sans-serif',color:'#fff',boxShadow:'0 4px 16px rgba(0,0,0,.4)',transition:'opacity .4s',pointerEvents:'none'});document.body.appendChild(bub)}
bub.textContent=t;bub.style.background=ok===false?'#d64545':ok?'#2f9e44':'#5a67f2';bub.style.opacity='1';
bub.style.pointerEvents=click?'auto':'none';bub.style.cursor=click?'pointer':'';bub.onclick=click||null;
if(ok!==undefined){const b=bub;setTimeout(()=>{b.style.opacity='0';setTimeout(()=>{b.remove();if(bub===b)bub=null},500)},ok?2500:6000)}};
const open=()=>window.open(APP+'/clip','mbclip','width=960,height=860');
const start=w=>{const html=document.documentElement.outerHTML;
const on=async e=>{if(e.source!==w||e.origin!==APP)return;const m=e.data||{};
if(m.type==='ready')w.postMessage({type:'page',url,html},APP);
if(m.type==='status'){say(String(m.text||'').slice(0,120),m.state==='saved'?true:m.state==='error'?false:undefined);if(m.state==='saved')removeEventListener('message',on)}
if(m.type==='fetch'&&Array.isArray(m.urls)){for(let i=0;i<m.urls.length;i++){const u=String(m.urls[i]);
try{const r=await fetch(u);if(!r.ok)throw 0;const b=await r.arrayBuffer();
w.postMessage({type:'file',i,url:u,mime:r.headers.get('content-type')||'',buf:b},APP,[b])}
catch(_){w.postMessage({type:'file-failed',i,url:u},APP)}}w.postMessage({type:'done'},APP)}};
addEventListener('message',on)};
const w=open();
if(w){start(w);return}
/* the browser blocked the window: a click on the page always may open one */
say('Click here to open the save window',undefined,()=>{const w2=open();if(w2){if(bub){bub.remove();bub=null}start(w2)}
else say('Allow pop-ups for this site, then try again.',false)});
}
