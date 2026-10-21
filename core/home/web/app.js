"use strict";
const $ = id => document.getElementById(id);
const labels = {on:"켜기",off:"끄기",mute:"음소거",unmute:"음소거 해제",play:"재생",pause:"일시정지",stop:"정지"};
let token = "", busy = false, uncertain = false;
function node(tag, text, className) { const el=document.createElement(tag); if(text!==undefined)el.textContent=text; if(className)el.className=className; return el; }
async function get(path) { const r=await fetch(path,{cache:"no-store"}); if(!r.ok)throw new Error("connection"); return r.json(); }
function disableActions() { document.querySelectorAll(".actions button").forEach(b=>b.disabled=busy||uncertain); }
async function refresh(keepNotice=false) {
  try {
    const data=await get("/api/home");
    $("connection").textContent=data.ready?"집 연결이 정상이에요":"집 연결을 확인해 주세요";
    $("connection-dot").className=data.ready?"dot ready":"dot";
    $("devices").replaceChildren();
    if(!data.devices.length)$("devices").append(node("p", "연결이 준비되면 기기가 여기에 표시돼요.", "empty"));
    data.devices.forEach(d=>{
      const card=node("article",undefined,"card"), top=node("div",undefined,"card-top");
      top.append(node("span",d.kind==="light"?"☼":d.name==="컴퓨터"?"▣":"◉","icon"),node("span",d.on===true?"켜짐":d.on===false?"꺼짐":"상태 미확인",d.on===true?"state on":"state"));
      card.append(top,node("h2",d.name),node("p",d.note,"note"));
      if(d.commands.length){const actions=node("div",undefined,"actions");d.commands.forEach(command=>{const b=node("button",labels[command]||command);b.type="button";b.dataset.command=command;b.addEventListener("click",()=>execute(d,command,card));actions.append(b);});card.append(actions);}
      else card.append(node("span","현재는 상태만 확인할 수 있어요","view-only"));
      $("devices").append(card);
    });
    disableActions();
    if(!keepNotice&&!busy&&!uncertain)$("notice").textContent=data.ready?"원하는 기기의 켜기·끄기를 눌러 주세요.":"오래된 상태로 기기를 제어하지 않아요.";
  }catch{ $("devices").replaceChildren(node("p","집 연결을 확인하지 못했어요.","empty")); $("connection").textContent="연결을 확인해 주세요"; $("connection-dot").className="dot"; }
}
async function execute(device,command,card) {
  if(busy||uncertain)return;
  busy=true;disableActions();card.classList.add("pending");
  const key=crypto.randomUUID().replaceAll("-","");
  $("notice").textContent=`${device.name} · ${labels[command]} 요청을 보내고 있어요.`;
  try {
    const response=await fetch("/api/commands",{method:"POST",headers:{"Content-Type":"application/json","X-Rio-Web-Token":token},body:JSON.stringify({kind:device.kind,target:device.name,command,request_id:key})});
    if(!response.ok)throw new Error("request");
    let answer=await response.json();
    for(let n=0;n<8&&answer.status==="DISPATCHED";n++){await new Promise(r=>setTimeout(r,1500));answer=await get("/api/results/"+key);}
    if(answer.status==="SUCCEEDED")$("notice").textContent=`${device.name} 요청이 완료됐어요. 실제 기기 상태도 확인해 주세요.`;
    else if(answer.status==="REJECTED"||answer.status==="PREVIEW")$("notice").textContent="현재 연결과 허용 상태로는 요청을 실행할 수 없어요.";
    else {uncertain=true;$("notice").textContent="요청 결과가 불확실해요. 다시 보내지 않고 실제 기기 상태를 확인해 주세요.";}
  }catch{uncertain=true;$("notice").textContent="요청 결과를 확인하지 못했어요. 같은 요청을 다시 보내지 않을게요.";}
  finally{busy=false;await refresh(true);disableActions();}
}
$("refresh").addEventListener("click",()=>refresh());
(async()=>{try{token=(await get("/api/session")).token;await refresh();}catch{$("notice").textContent="화면을 다시 열어 연결을 확인해 주세요.";}})();
