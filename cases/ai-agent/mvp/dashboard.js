"use strict";
const $=id=>document.getElementById(id);
let token="",selected=null,signals=[];
function element(tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=String(text);if(className)el.className=className;return el;}
async function request(path,options={}){const headers={...(options.headers||{})};if(token)headers["X-Review-Token"]=token;if(options.body)headers["Content-Type"]="application/json";const r=await fetch(path,{...options,headers,cache:"no-store"});if(!r.ok){let message="Ошибка "+r.status;try{message=(await r.json()).detail||message}catch{}throw Error(message)}return r.json()}
function stats(){for(const [key,n] of [["total",signals.length],["pending",signals.filter(s=>s.status==="pending_human_review").length],["approved",signals.filter(s=>s.status==="approved").length],["rejected",signals.filter(s=>s.status==="rejected").length]])$(key).textContent=n}
function label(s){return s==="approved"?"Подтвержден":s==="rejected"?"Отклонен":"Ожидает проверки"}
function renderList(){const root=$("signals");root.replaceChildren();const search=$("search").value.toLocaleLowerCase("ru");const status=$("status").value;const rows=signals.filter(s=>(!status||s.status===status)&&[s.product,s.category,s.workflow].join(" ").toLocaleLowerCase("ru").includes(search));if(!rows.length){root.append(element("p","Нет подходящих сигналов","empty"));return}for(const s of rows){const b=element("button",undefined,"signal"+(selected===s.id?" selected":""));b.type="button";b.append(element("span",label(s.status),"tag "+s.status),element("strong",s.product+" · "+s.workflow),element("small",s.category+" · "+s.ticket_count+" обращений"));b.addEventListener("click",()=>openDetail(s.id));root.append(b)}}
async function refresh(){try{signals=await request("/signals");stats();renderList();if(selected&&signals.some(s=>s.id===selected))await openDetail(selected)}catch(e){$("signals").replaceChildren(element("p",e.message,"empty error"))}}
async function openDetail(id){selected=id;renderList();const root=$("detail");root.replaceChildren(element("p","Загрузка доказательств…","empty"));try{const [s,history]=await Promise.all([request("/signals/"+encodeURIComponent(id)),request("/signals/"+encodeURIComponent(id)+"/timeline")]);root.replaceChildren();root.append(element("span",label(s.status),"tag "+s.status),element("h3",s.product+" · "+s.workflow),element("p",s.category+" · "+s.ticket_count+" обращений · "+s.method,"meta"));root.append(element("h4","Примеры обращений"));for(const example of s.examples)root.append(element("div",example,"evidence"));root.append(element("h4","Идентификаторы источников"),element("p",s.ticket_ids.join(", ")||"Нет данных","meta"));root.append(element("h4","История сигнала"));const ul=element("ol",undefined,"timeline");for(const event of history){const line=event.type==="evidence_added"?"Добавлены обращения: "+event.added_ticket_ids.join(", "):"Решение: "+event.action+" · "+event.reason;ul.append(element("li",event.at+" — "+line))}root.append(ul);if(s.status==="pending_human_review"){const actions=element("div",undefined,"actions"),reason=element("textarea");reason.placeholder="Основание решения (не менее 3 символов)";const buttons=element("div",undefined,"buttons");for(const [action,title,style] of [["approved","Подтвердить",""],["rejected","Отклонить","reject"]]){const b=element("button",title,style);b.addEventListener("click",async()=>{if(reason.value.trim().length<3){reason.focus();return}b.disabled=true;try{await request("/signals/"+encodeURIComponent(id)+"/review",{method:"POST",body:JSON.stringify({action,reason:reason.value.trim()})});await refresh()}catch(e){alert(e.message)}finally{b.disabled=false}});buttons.append(b)}actions.append(reason,buttons);root.append(actions)}}catch(e){root.replaceChildren(element("p",e.message,"empty error"))}}
$("connect").addEventListener("click",()=>{token=$("token").value;$("token").value="";refresh()});$("refresh").addEventListener("click",refresh);$("status").addEventListener("change",renderList);$("search").addEventListener("input",renderList);refresh();

$("import-file").addEventListener("change",async event=>{
 const file=event.target.files[0];if(!file)return;
 if(file.size>1024*1024){$("import-result").textContent="Файл превышает 1 МБ";return}
 $("import-json").value=await file.text();
});
$("import-submit").addEventListener("click",async()=>{
 const output=$("import-result"),button=$("import-submit");
 button.disabled=true;
 try{
  const tickets=JSON.parse($("import-json").value);
  if(!Array.isArray(tickets)||tickets.length<1||tickets.length>500)throw Error("Нужен массив из 1–500 обращений");
  const result=await request("/imports",{method:"POST",body:JSON.stringify({tickets})});
  output.textContent="Обработано: "+result.processed+" · Сигналов: "+result.signals_found+" · Новых: "+result.new_signals;
  await refresh();
 }catch(error){output.textContent="Ошибка импорта: "+error.message}
 finally{button.disabled=false}
});
