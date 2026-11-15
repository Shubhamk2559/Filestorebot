"""utils/ads_page.py - the Telegram Mini App page (served at /ad). Static: all data comes from the signed API.

Theme matches the landing page (utils/home.py): neon pink / dark, glowing rotating ring, floating hearts, Pacifico title.
Only markup + CSS changed - the JS flow below is untouched (same element ids: icon, title, text, steps, spin, btn).
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#0a0107">
<title>Unlock your files</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Pacifico&display=swap" rel="stylesheet">
<style>
  @property --a{syntax:'<angle>';initial-value:0deg;inherits:false}
  :root{--pink:#ff2d95;--pink2:#ff7ccf;--wine:#7a0b3c;--bg:#0a0107;--muted:#f0c9dd}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;min-height:100%;background:var(--bg);color:#fff;
    font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  body{background:radial-gradient(800px 520px at 50% -8%,rgba(255,45,149,.5),transparent 70%),
    radial-gradient(600px 460px at 0% 70%,rgba(190,20,90,.35),transparent 70%),
    radial-gradient(700px 520px at 100% 100%,rgba(150,30,255,.3),transparent 70%),var(--bg);
    min-height:100vh;overflow-x:hidden}
  body:after{content:"";position:fixed;inset:0;pointer-events:none;box-shadow:inset 0 0 140px 30px rgba(0,0,0,.75);z-index:0}
  .fx{position:fixed;inset:0;pointer-events:none;overflow:hidden;z-index:0}
  .fx i{position:absolute;bottom:-30px;color:var(--pink2);opacity:0;font-style:normal;text-shadow:0 0 12px var(--pink);animation:rise linear infinite}
  @keyframes rise{0%{transform:translateY(0) rotate(0) scale(.8);opacity:0}12%{opacity:.85}100%{transform:translateY(-105vh) rotate(25deg) scale(1.25);opacity:0}}
  main{position:relative;z-index:1;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
    padding:max(24px,env(safe-area-inset-top)) 18px max(24px,env(safe-area-inset-bottom))}
  .crown{font-size:30px;filter:drop-shadow(0 0 12px var(--pink));margin-bottom:-4px;animation:flick 5s infinite}
  @keyframes flick{0%,100%{opacity:1}46%{opacity:1}48%{opacity:.55}50%{opacity:1}52%{opacity:.7}54%{opacity:1}}
  .ringwrap{width:104px;height:104px;border-radius:50%;padding:4px;margin:8px 0 16px;
    background:conic-gradient(from var(--a),#ff2d95,#ffd1ee,#b400ff,#ff2d95,#ff7ccf,#ff2d95);
    animation:spin 5s linear infinite,glow 3s ease-in-out infinite;
    box-shadow:0 0 22px 4px rgba(255,45,149,.65),0 0 60px 12px rgba(255,45,149,.35)}
  @keyframes spin{to{--a:360deg}}
  @keyframes glow{50%{box-shadow:0 0 34px 8px rgba(255,45,149,.9),0 0 90px 20px rgba(255,45,149,.45)}}
  .ring{width:100%;height:100%;border-radius:50%;overflow:hidden;background:#000;border:3px solid #12020b}
  .ring img{width:100%;height:100%;object-fit:cover;display:block}
  .card{position:relative;width:100%;max-width:380px;padding:26px 22px 24px;text-align:center;border-radius:24px;
    background:rgba(255,45,149,.09);border:1px solid rgba(255,124,207,.4);
    backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
    box-shadow:0 0 30px rgba(255,45,149,.22) inset,0 18px 50px rgba(0,0,0,.45),0 0 40px rgba(255,45,149,.18)}
  .icon{font-size:46px;line-height:1;margin-bottom:12px;filter:drop-shadow(0 0 14px rgba(255,45,149,.9));animation:bob 2.6s ease-in-out infinite}
  @keyframes bob{50%{transform:translateY(-5px) scale(1.06)}}
  h1{margin:0 0 8px;font-family:Pacifico,"Brush Script MT",cursive;font-weight:400;font-size:1.75rem;line-height:1.2;
    color:transparent;background:linear-gradient(100deg,#ff2d95 20%,#fff 45%,#ff2d95 55%,#ff1f6e 80%);background-size:250% 100%;
    -webkit-background-clip:text;background-clip:text;animation:shine 3.6s linear infinite;
    filter:drop-shadow(0 0 12px rgba(255,45,149,.8))}
  @keyframes shine{from{background-position:150% 0}to{background-position:-100% 0}}
  p{margin:0 0 6px;color:var(--muted);font-size:15px;line-height:1.6;white-space:pre-line}
  .spin{width:44px;height:44px;margin:20px auto 4px;border-radius:50%;
    background:conic-gradient(from 0deg,transparent 0 25%,var(--pink) 80%,#fff);
    -webkit-mask:radial-gradient(farthest-side,transparent calc(100% - 5px),#000 calc(100% - 4px));
    mask:radial-gradient(farthest-side,transparent calc(100% - 5px),#000 calc(100% - 4px));
    filter:drop-shadow(0 0 8px rgba(255,45,149,.9));animation:r .9s linear infinite}
  @keyframes r{to{transform:rotate(360deg)}}
  button{position:relative;overflow:hidden;margin-top:20px;width:100%;border:0;border-radius:16px;padding:15px;
    font:800 1.05rem system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;letter-spacing:.3px;color:#fff;cursor:pointer;
    background:linear-gradient(180deg,#ff4fa8,#e0187c);
    box-shadow:0 0 0 2px rgba(255,255,255,.16),0 10px 28px rgba(255,45,149,.55),0 0 40px rgba(255,45,149,.35);
    animation:pulse 2.2s ease-in-out infinite}
  button:after{content:"";position:absolute;top:0;left:-60%;width:40%;height:100%;
    background:linear-gradient(100deg,transparent,rgba(255,255,255,.55),transparent);transform:skewX(-20deg);animation:sweep 3s ease-in-out infinite}
  @keyframes sweep{0%,55%{left:-60%}100%{left:130%}}
  @keyframes pulse{50%{transform:scale(1.03)}}
  button:active{transform:scale(.97)}
  .hide{display:none!important}
  .steps{text-align:left;margin:16px 0 0;padding:12px 14px 12px 32px;color:var(--muted);font-size:14px;line-height:1.55;
    background:rgba(0,0,0,.28);border:1px solid rgba(255,124,207,.28);border-radius:14px}
  .steps li{margin:5px 0}.steps li::marker{color:var(--pink2)}.steps b{color:#fff}
  .on{margin-top:18px;color:var(--pink2);font-family:Pacifico,cursive;font-size:1.05rem;text-shadow:0 0 14px rgba(255,45,149,.8)}
  .on b{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3dff8b;margin-right:8px;box-shadow:0 0 10px #3dff8b;animation:blink 1.6s infinite}
  @keyframes blink{50%{opacity:.35}}
  @media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style>
</head>
<body>
<div class="fx">
  <i style="left:6%;animation-delay:0s;animation-duration:11s;font-size:16px">♥</i>
  <i style="left:18%;animation-delay:3s;animation-duration:14s;font-size:13px">✦</i>
  <i style="left:30%;animation-delay:6s;animation-duration:12s;font-size:18px">💋</i>
  <i style="left:44%;animation-delay:1s;animation-duration:15s;font-size:14px">♥</i>
  <i style="left:57%;animation-delay:4s;animation-duration:10s;font-size:20px">✦</i>
  <i style="left:68%;animation-delay:7s;animation-duration:13s;font-size:13px">♥</i>
  <i style="left:79%;animation-delay:2s;animation-duration:16s;font-size:17px">💋</i>
  <i style="left:88%;animation-delay:5s;animation-duration:12s;font-size:12px">✦</i>
  <i style="left:96%;animation-delay:8s;animation-duration:14s;font-size:15px">♥</i>
</div>
<main>
  <div class="crown">👑</div>
  <div class="ringwrap"><div class="ring"><img src="/assets/home-image" alt="" width="104" height="104" onerror="this.parentNode.parentNode.style.display='none'"></div></div>
  <div class="card">
    <div class="icon" id="icon">⏳</div>
    <h1 id="title">Please wait…</h1>
    <p id="text"></p>
    <ol class="steps hide" id="steps">
      <li>Turn off any ad blocker or "Block ads" feature</li>
      <li>Settings → Network → <b>Private DNS</b> → set to <b>Off</b> / Automatic (AdGuard, NextDNS etc. block ads)</li>
      <li>Turn off ad-blocking VPN / DNS apps (Blokada, AdGuard, DNS66…)</li>
      <li>Then tap <b>Check again</b></li>
    </ol>
    <div class="spin" id="spin"></div>
    <button id="btn" class="hide">Check again</button>
  </div>
  <div class="on"><b></b>Always Online ♡</div>
</main>
<script>
(function(){
"use strict";
var tg = window.Telegram && window.Telegram.WebApp;
var $ = function(id){return document.getElementById(id)};
var sid = new URLSearchParams(location.search).get("s") || "";
var BOOT = __BOOT__;               /* settings baked in by the server -> no init round trip before we start */
var PRELOAD = true;                /* ask the ad SDK to fetch the ad in the background while we check things */
var cfg = BOOT, sdkReady = false, sdkTag = null;

function sleep(ms){return new Promise(function(r){setTimeout(r,ms)})}
function ui(icon,title,text,o){
  o=o||{};
  $("icon").textContent=icon; $("title").textContent=title; $("text").textContent=text||"";
  $("spin").classList.toggle("hide",!o.spin);
  $("steps").classList.toggle("hide",!o.steps);
  var b=$("btn"); b.classList.toggle("hide",!o.btn); if(o.btn) b.textContent=o.btn;
}
function waitClick(){return new Promise(function(res){var b=$("btn");b.onclick=function(){b.onclick=null;res()}})}

async function api(path,data){
  var body=Object.assign({sid:sid,initData:tg.initData},data||{});
  var r=await fetch("/api/ad/"+path,{method:"POST",cache:"no-store",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(!r.ok) throw new Error("http "+r.status);
  return r.json();
}

/* ---------------------------------------------------------------- ad blocker / DNS detection */
async function ping(url,ms){
  var c=new AbortController(), t=setTimeout(function(){c.abort()},ms||2500);
  try{
    await fetch(url+(url.indexOf("?")>-1?"&":"?")+"_="+Date.now(),{mode:"no-cors",cache:"no-store",signal:c.signal});
    return "ok";
  }catch(e){ return e&&e.name==="AbortError"?"timeout":"fail"; }
  finally{ clearTimeout(t); }
}
function baitHidden(){
  return new Promise(function(res){
    var d=document.createElement("div");
    d.className="adsbox ad-banner ads advert textads banner-ads pub_300x250 sponsor ad-placement";
    d.style.cssText="position:absolute;left:-9999px;top:-9999px;width:10px;height:10px";
    d.innerHTML="&nbsp;"; document.body.appendChild(d);
    setTimeout(function(){
      var cs=getComputedStyle(d);
      var hidden=!d.offsetHeight||cs.display==="none"||cs.visibility==="hidden";
      d.remove(); res(hidden);
    },150);
  });
}
async function detect(){
  var sdkHost=new URL(cfg.sdk).origin+"/";
  var r=await Promise.all([ping("/health",4000),baitHidden(),ping(sdkHost)].concat(cfg.probes.map(function(u){return ping(u)})));
  var ctrl=r[0], bait=r[1], sdk=r[2], gen=r.slice(3);
  if(ctrl!=="ok") return {offline:true,blocked:false,detail:"offline"};
  var genFail=gen.filter(function(x){return x==="fail"}).length;
  var why=[];
  if(bait) why.push("bait");
  if(sdk==="fail") why.push("sdkhost");
  if(genFail>=2) why.push("dns"+genFail);
  return {offline:false,blocked:why.length>0,detail:why.join(",")};
}
function loadSdk(){
  return new Promise(function(res,rej){
    if(sdkTag) sdkTag.remove();
    var s=document.createElement("script");
    s.src=cfg.sdk; s.async=true;
    s.setAttribute("data-zone",cfg.zone); s.setAttribute("data-sdk","show_"+cfg.zone);
    s.onload=function(){ typeof window["show_"+cfg.zone]==="function"?res():rej(new Error("sdk_missing")) };
    s.onerror=function(){ rej(new Error("sdk_blocked")) };
    sdkTag=s; document.head.appendChild(s);
  });
}

/* ---------------------------------------------------------------- main flow */
function showBlocked(){
  ui("🚫","Ad blocker detected","Files are unlocked only after watching the ad. Please disable the ad blocker / ad-blocking DNS:",
     {steps:true,btn:"Check again"});
}
async function claim(){
  ui("📦","Sending your files…","",{spin:true});
  var c=await api("claim");
  if(c.ok){
    ui("✅","Done!","Your files are being sent to the chat.");
    setTimeout(function(){try{tg.close()}catch(e){}},500);
  }else{
    ui("⚠️","Could not unlock","Please go back to the chat and open your file link again.");
  }
}
async function pollReady(sec){
  /* the server holds each status call open until the ad is confirmed -> we continue the instant it is */
  var end=Date.now()+sec*1000;
  while(Date.now()<end){
    try{
      var r=await api("status",{wait:1});
      if(r.state==="ready") return true;
      if(r.state==="closed") return false;
    }catch(e){ await sleep(500); }
    await sleep(120);
  }
  return false;
}
async function ensureSdk(){
  if(sdkReady) return true;
  try{ await loadSdk(); sdkReady=true; return true; }catch(e){ return false; }
}
function preloadAd(){
  if(!PRELOAD||!sdkReady) return;
  try{
    var p=window["show_"+cfg.zone]({type:"preload",ymid:sid,requestVar:"files"});
    if(p&&p.catch) p.catch(function(){});
  }catch(e){}
}
function finalMsg(state){
  if(state==="no_fallback") ui("⏳","No ads available right now","Please try again a little later.");
  else if(state==="too_many") ui("⚠️","Too many tries","Go back to the chat and open your file link again.");
  else ui("⌛","Session ended","Go back to the chat and open your file link again.");
}

async function run(){
  if(!tg||!tg.initData){ ui("⚠️","Open inside Telegram","This page works only from the bot's button in Telegram."); return; }
  try{tg.ready();tg.expand();if(tg.disableVerticalSwipes)tg.disableVerticalSwipes();}catch(e){}
  ui("🔍","Checking your connection…","",{spin:true});

  /* start everything at the same time instead of one step after another */
  var initP = api("init");
  var envP  = detect();
  var sdkP  = ensureSdk().then(function(ok){ if(ok) preloadAd(); return ok; });

  var st;
  try{ st=await initP; }catch(e){ ui("⚠️","Session invalid","Go back to the chat and open your file link again."); return; }
  if(st.state==="done"){ ui("✅","Already sent","Your files were already sent to the chat."); return; }
  if(st.state!=="open"){ finalMsg("closed"); return; }

  var first=true;
  for(;;){
    var env, sdkOk;
    if(first){ env=await envP; sdkOk=await sdkP; first=false; }
    else{
      ui("🔍","Checking your connection…","",{spin:true});
      env=await detect(); sdkOk=await ensureSdk();
    }
    if(env.offline){ ui("📡","No internet","Check your connection and try again.",{btn:"Try again"}); await waitClick(); continue; }
    if(cfg.strict && env.blocked){
      try{await api("probe",{blocked:true,detail:env.detail});}catch(e){}
      showBlocked(); await waitClick(); continue;
    }
    if(!sdkOk && cfg.strict){ try{await api("probe",{blocked:true,detail:"sdk"});}catch(_){} showBlocked(); await waitClick(); continue; }
    if(sdkOk) preloadAd();

    var a=await api("attempt",{blocked:false,detail:""});   /* the clean probe result rides along */
    if(!a.ok){
      if(a.state==="blocked"){ showBlocked(); await waitClick(); continue; }
      if(a.state==="slow_down"){ await sleep(1200); continue; }
      finalMsg(a.state); return;
    }

    ui("🎬","Loading ad…","Watch the ad till the end.",{spin:true});
    var shown=false, reason="";
    try{
      if(!sdkReady) throw new Error("Network error");
      await window["show_"+cfg.zone]({ymid:a.ymid,requestVar:"files"});
      shown=true;
    }catch(e){ reason=String((e&&e.message)||e); }

    if(shown){
      var r=await api("shown");
      if(r.state==="too_fast"){ ui("⏱","Please watch the full ad","The ad must play till the end.",{spin:true}); await sleep(2500); continue; }
      ui("🔐","Verifying…","",{spin:true});
      if(r.state==="ready" || await pollReady(30)){ await claim(); return; }
      ui("⚠️","Ad could not be verified","Trying again…",{spin:true}); await sleep(1200); continue;
    }

    /* the ad did not show: ad blocker, or genuinely no ad available? */
    var env2=await detect();
    if(cfg.strict && env2.blocked){
      try{await api("probe",{blocked:true,detail:env2.detail});}catch(e){}
      sdkReady=false; showBlocked(); await waitClick(); continue;
    }
    await api("probe",{blocked:false,detail:""});
    var f=await api("fail",{reason:reason});
    if(f.state==="ready"){ await claim(); return; }
    if(f.state==="blocked"){ showBlocked(); await waitClick(); continue; }
    if(f.state==="no_fallback"||f.state==="closed"){ finalMsg(f.state); return; }
    if(f.state==="retry" && f.fails>0){
      ui("🔄","No ad available","Trying again ("+f.fails+"/"+cfg.tries+")…",{spin:true});
    }else{
      ui("🔄","Ad was closed","Please watch the full ad to unlock your files.",{spin:true});
    }
    await sleep(1500);
  }
}
run().catch(function(e){ ui("⚠️","Something went wrong","Close this window and open your file link again."); });
})();
</script>
</body>
</html>
"""
