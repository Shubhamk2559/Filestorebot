"""utils/ads_page.py - the Telegram Mini App page (served at /ad). Static: all data comes from the signed API.

Theme: plain light sky blue (solid + soft gradient), no neon / glow, no emojis - only the round PNG picture,
title, text and one spinner. Only markup + CSS changed - the JS flow below is untouched (same element ids: icon, title, text, steps, spin, btn).
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#d6eefb">
<title>Unlock your files</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root{--sky:#87cefa;--sky-d:#4fb0ec;--ink:#12354d;--muted:#4d7088;--bg1:#eaf6fe;--bg2:#c9e8fb}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;min-height:100%;color:var(--ink);
    font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  body{min-height:100vh;background:linear-gradient(180deg,var(--bg1),var(--bg2));overflow-x:hidden}
  main{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
    padding:max(24px,env(safe-area-inset-top)) 18px max(24px,env(safe-area-inset-bottom))}
  .ringwrap{width:104px;height:104px;border-radius:50%;padding:4px;margin:0 0 18px;background:var(--sky)}
  .ring{width:100%;height:100%;border-radius:50%;overflow:hidden;background:#fff;border:3px solid #fff}
  .ring img{width:100%;height:100%;object-fit:cover;display:block}
  .card{width:100%;max-width:380px;padding:26px 22px 24px;text-align:center;border-radius:20px;
    background:#fff;border:1px solid #b5defa}
  .icon{display:none}
  h1{margin:0 0 8px;font-weight:700;font-size:1.4rem;line-height:1.25;color:var(--ink)}
  p{margin:0 0 6px;color:var(--muted);font-size:15px;line-height:1.6;white-space:pre-line}
  .spin{width:40px;height:40px;margin:20px auto 4px;border-radius:50%;
    border:4px solid #d6eefb;border-top-color:var(--sky-d);animation:r .9s linear infinite}
  @keyframes r{to{transform:rotate(360deg)}}
  button{margin-top:20px;width:100%;border:0;border-radius:14px;padding:15px;
    font:700 1.05rem system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--ink);cursor:pointer;
    background:var(--sky)}
  button:active{background:var(--sky-d)}
  .hide{display:none!important}
  .steps{text-align:left;margin:16px 0 0;padding:12px 14px 12px 32px;color:var(--muted);font-size:14px;line-height:1.55;
    background:#eef8fe;border:1px solid #cfe8f9;border-radius:12px}
  .steps li{margin:5px 0}.steps b{color:var(--ink)}
  .guide{text-align:left;margin:18px 0 0}
  .gt{font-weight:700;font-size:1.05rem;color:var(--ink);margin:0 0 4px}
  .gs{font-size:13.5px;line-height:1.5;margin:0 0 10px}
  .gstep{margin:0 0 16px}
  .gh{display:flex;align-items:flex-start;gap:10px;font-size:14.5px;line-height:1.45;color:var(--ink)}
  .gh .n{flex:0 0 26px;height:26px;border-radius:50%;background:var(--sky);color:var(--ink);font-weight:700;font-size:14px;
    display:flex;align-items:center;justify-content:center}
  .gh span:last-child{padding-top:2px}
  .gimg{display:block;width:100%;height:auto;margin-top:8px;border-radius:14px;border:1px solid #cfe8f9;background:#eef8fe}
  .note{margin:4px 0 0;padding:12px 14px;border-radius:12px;background:#eef8fe;border:1px solid #cfe8f9;
    color:var(--muted);font-size:14px;line-height:1.55}
  .note b{color:var(--ink)}
  .note ol{margin:6px 0 8px;padding-left:20px}.note li{margin:3px 0}
  button.alt{margin-top:10px;background:#fff;border:2px solid var(--sky)}
  button.alt:active{background:#eef8fe}
  .on{margin-top:18px;color:var(--muted);font-size:14px;font-weight:600}
  .on b{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--sky-d);margin-right:8px}
  @media (prefers-reduced-motion:reduce){.spin{animation-duration:2.4s}}
</style>
</head>
<body>
<main>
  <div class="ringwrap"><div class="ring"><img src="/assets/home-image" alt="" width="104" height="104" onerror="this.parentNode.parentNode.style.display='none'"></div></div>
  <div class="card">
    <div class="icon" id="icon"></div>
    <h1 id="title">Please wait…</h1>
    <p id="text"></p>
    <ol class="steps hide" id="steps">
      <li>Turn off any ad blocker or "Block ads" feature</li>
      <li>Settings → Network → <b>Private DNS</b> → set to <b>Off</b> / Automatic (AdGuard, NextDNS etc. block ads)</li>
      <li>Turn off ad-blocking VPN / DNS apps (Blokada, AdGuard, DNS66…)</li>
      <li>Then tap <b>Check again</b></li>
    </ol>
    <div class="guide hide" id="guide">
      <div class="gt">How to turn off Private DNS</div>
      <p class="gs">Follow the pictures below. Menu names can look a little different on your phone.</p>
      <div class="gstep">
        <div class="gh"><span class="n">1</span><span>Open your phone <b>Settings</b></span></div>
        <img class="gimg" src="/assets/guide/step1-settings.png" width="1189" height="1323" loading="lazy" decoding="async" alt="Phone Settings screen">
      </div>
      <div class="gstep">
        <div class="gh"><span class="n">2</span><span>Type <b>Private DNS</b> in the search bar and open it</span></div>
        <img class="gimg" src="/assets/guide/step2-search.png" width="1189" height="1323" loading="lazy" decoding="async" alt="Searching for Private DNS in Settings">
      </div>
      <div class="gstep">
        <div class="gh"><span class="n">3</span><span>If <b>Private DNS provider</b> is selected (for example <b>dns.adguard.com</b>), this is what blocks the ad</span></div>
        <img class="gimg" src="/assets/guide/step3-provider.png" width="1189" height="1323" loading="lazy" decoding="async" alt="Private DNS provider is selected">
      </div>
      <div class="gstep">
        <div class="gh"><span class="n">4</span><span>Select <b>Off</b></span></div>
        <img class="gimg" src="/assets/guide/step4-off.png" width="1189" height="1323" loading="lazy" decoding="async" alt="Private DNS set to Off">
      </div>
      <div class="note">
        <b>After you turn it Off:</b>
        <ol>
          <li>Close Telegram completely, then open it again.</li>
          <li>Tap your file link again.</li>
        </ol>
        Still not working? Send a screenshot to <b>Support</b>.
      </div>
    </div>
    <div class="spin" id="spin"></div>
    <button id="btn" class="hide">Check again</button>
    <button id="sup" class="alt hide">Contact Support</button>
  </div>
  <div class="on"><b></b>Always Online</div>
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
  $("title").textContent=title; $("text").textContent=text||"";
  $("spin").classList.toggle("hide",!o.spin);
  $("steps").classList.toggle("hide",!o.steps);
  $("guide").classList.toggle("hide",!o.steps);
  $("sup").classList.toggle("hide",!(o.steps&&cfg.support));
  var b=$("btn"); b.classList.toggle("hide",!o.btn); if(o.btn) b.textContent=o.btn;
}
function openSupport(){
  if(!cfg.support) return;
  try{ if(tg&&tg.openTelegramLink){ tg.openTelegramLink(cfg.support); return; } }catch(e){}
  window.open(cfg.support,"_blank");
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
  /* returns true when the flow is finished, false when another ad must be watched */
  ui("📦","Sending your files…","",{spin:true});
  var c=await api("claim");
  if(c.ok && c.state==="next"){
    ui("✅","Ad "+c.done+" of "+c.need+" done","Next ad loading…",{spin:true});
    await sleep(900);
    return false;
  }
  if(c.ok){
    ui("✅","Done!","Your files are being sent to the chat.");
    setTimeout(function(){try{tg.close()}catch(e){}},500);
  }else{
    ui("⚠️","Could not unlock","Please go back to the chat and open your file link again.");
  }
  return true;
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

$("sup").onclick=openSupport;

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
      if(r.state==="ready" || await pollReady(30)){ if(await claim()) return; continue; }
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
    if(f.state==="ready"){ if(await claim()) return; continue; }
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
