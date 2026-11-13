"""utils/ads_page.py - the Telegram Mini App page (served at /ad). Static: all data comes from the signed API."""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>Unlock your files</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root{
    --bg:var(--tg-theme-bg-color,#10141b);--fg:var(--tg-theme-text-color,#eef1f6);
    --hint:var(--tg-theme-hint-color,#8b95a7);--btn:var(--tg-theme-button-color,#3b82f6);
    --btn-fg:var(--tg-theme-button-text-color,#fff);--card:var(--tg-theme-secondary-bg-color,#1a2029);
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
    font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  main{min-height:100%;display:flex;align-items:center;justify-content:center;padding:24px 16px}
  .card{width:100%;max-width:380px;background:var(--card);border-radius:20px;padding:28px 22px;text-align:center}
  .icon{font-size:48px;line-height:1;margin-bottom:14px}
  h1{font-size:20px;margin:0 0 8px}
  p{margin:0 0 6px;color:var(--hint);font-size:15px;white-space:pre-line}
  .spin{width:34px;height:34px;margin:18px auto 4px;border-radius:50%;border:3px solid var(--hint);
    border-top-color:var(--btn);animation:r 0.9s linear infinite}
  @keyframes r{to{transform:rotate(360deg)}}
  button{margin-top:18px;width:100%;border:0;border-radius:12px;padding:14px;font-size:16px;font-weight:600;
    background:var(--btn);color:var(--btn-fg);cursor:pointer}
  .hide{display:none!important}
  .steps{text-align:left;margin:14px 0 0;padding:0 0 0 18px;color:var(--hint);font-size:14px}
</style>
</head>
<body>
<main>
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
</main>
<script>
(function(){
"use strict";
var tg = window.Telegram && window.Telegram.WebApp;
var $ = function(id){return document.getElementById(id)};
var sid = new URLSearchParams(location.search).get("s") || "";
var cfg = null, sdkReady = false, sdkTag = null;

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
  var c=new AbortController(), t=setTimeout(function(){c.abort()},ms||7000);
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
  var r=await Promise.all([ping("/health"),baitHidden(),ping(sdkHost)].concat(cfg.probes.map(function(u){return ping(u)})));
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
    setTimeout(function(){try{tg.close()}catch(e){}},1400);
  }else{
    ui("⚠️","Could not unlock","Please go back to the chat and open your file link again.");
  }
}
async function pollReady(sec){
  for(var i=0;i<sec;i++){
    var r=await api("status");
    if(r.state==="ready") return true;
    if(r.state==="closed") return false;
    await sleep(1000);
  }
  return false;
}
function finalMsg(state){
  if(state==="no_fallback") ui("⏳","No ads available right now","Please try again a little later.");
  else if(state==="too_many") ui("⚠️","Too many tries","Go back to the chat and open your file link again.");
  else ui("⌛","Session ended","Go back to the chat and open your file link again.");
}

async function run(){
  if(!tg||!tg.initData){ ui("⚠️","Open inside Telegram","This page works only from the bot's button in Telegram."); return; }
  try{tg.ready();tg.expand();if(tg.disableVerticalSwipes)tg.disableVerticalSwipes();}catch(e){}
  try{ cfg=await api("init"); }catch(e){ ui("⚠️","Session invalid","Go back to the chat and open your file link again."); return; }
  if(cfg.state==="done"){ ui("✅","Already sent","Your files were already sent to the chat."); return; }
  if(cfg.state!=="open"){ finalMsg("closed"); return; }

  for(;;){
    ui("🔍","Checking your connection…","",{spin:true});
    var env=await detect();
    if(env.offline){ ui("📡","No internet","Check your connection and try again.",{btn:"Try again"}); await waitClick(); continue; }
    if(cfg.strict && env.blocked){
      try{await api("probe",{blocked:true,detail:env.detail});}catch(e){}
      showBlocked(); await waitClick(); continue;
    }
    await api("probe",{blocked:false,detail:""});

    if(!sdkReady){
      try{ await loadSdk(); sdkReady=true; }
      catch(e){
        if(cfg.strict){ try{await api("probe",{blocked:true,detail:"sdk"});}catch(_){} showBlocked(); await waitClick(); continue; }
      }
    }

    var a=await api("attempt");
    if(!a.ok){
      if(a.state==="blocked"){ showBlocked(); await waitClick(); continue; }
      if(a.state==="slow_down"){ await sleep(2200); continue; }
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
      ui("⚠️","Ad could not be verified","Trying again…",{spin:true}); await sleep(2500); continue;
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
    await sleep(3000);
  }
}
run().catch(function(e){ ui("⚠️","Something went wrong","Close this window and open your file link again."); });
})();
</script>
</body>
</html>
"""
