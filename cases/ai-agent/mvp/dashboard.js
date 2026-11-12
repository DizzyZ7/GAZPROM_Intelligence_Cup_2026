"use strict";
const $=id=>document.getElementById(id);
let token="",selected=null,signals=[];
let detailLoad=0,listLoad=0;
const decisions={
 in_progress:"Взято в работу",
 known:"Тема уже известна",
 needs_clarification:"Нужно уточнение",
};
function element(tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=String(text);if(className)el.className=className;return el;}
async function request(path,options={}){
 const headers={...(options.headers||{})};
 if(token)headers["X-Review-Token"]=token;
 if(options.body)headers["Content-Type"]="application/json";
 const response=await fetch(path,{...options,headers,cache:"no-store"});
 if(!response.ok){
  let message="Ошибка "+response.status;
  try{message=(await response.json()).detail||message}catch{}
  const error=new Error(message);
  error.status=response.status;
  throw error;
 }
 return response.json();
}

function post(path,body){
 return request(path,{method:"POST",body:JSON.stringify(body)});
}

function stats(){for(const [key,n] of [["total",signals.length],["pending",signals.filter(s=>s.status==="pending_human_review").length],["approved",signals.filter(s=>s.status==="approved").length],["rejected",signals.filter(s=>s.status==="rejected").length]])$(key).textContent=n}
function label(s){return s==="approved"?"Подтвержден":s==="rejected"?"Отклонен":"Ожидает проверки"}
function renderList(){const root=$("signals");root.replaceChildren();const search=$("search").value.toLocaleLowerCase("ru");const status=$("status").value;const rows=signals.filter(s=>(!status||s.status===status)&&[s.product,s.category,s.workflow].join(" ").toLocaleLowerCase("ru").includes(search));if(!rows.length){root.append(element("p","Нет подходящих сигналов","empty"));return}for(const s of rows){const b=element("button",undefined,"signal"+(selected===s.id?" selected":""));b.type="button";b.append(element("span",label(s.status),"tag "+s.status),element("strong",s.product+" · "+s.workflow),element("small",s.category+" · "+s.ticket_count+" обращений"));b.addEventListener("click",()=>openDetail(s.id));root.append(b)}}

async function refresh(notice){
 const load=++listLoad;
 try{
  const [cards,queue]=await Promise.all([request("/signals"),request("/product-queue")]);
  if(load!==listLoad)return;
  signals=cards;
  stats();
  renderList();
  renderQueue(queue);
  if(selected)await openDetail(selected,notice?.id===selected?notice.message:undefined);
 }catch(error){
  if(load!==listLoad)return;
  signals=[];
  selected=null;
  detailLoad++;
  stats();
  $("signals").replaceChildren(element("p",error.message,"empty error"));
  $("detail").replaceChildren(element("p","Выберите сигнал слева","empty"));
  $("product-queue").replaceChildren(element("p",error.message,"empty error"));
 }
}

function feedback(){
 const status=element("p",undefined,"message error");
 status.setAttribute("role","status");
 return status;
}

function reasonInput(label){
 const input=element("textarea");
 input.placeholder=label+" (не менее 3 символов)";
 input.setAttribute("aria-label",label);
 input.required=true;
 input.minLength=3;
 input.maxLength=1000;
 return input;
}

async function changeCard(card,action,body,status,buttons){
 const enabled=buttons.filter(button=>!button.disabled);
 enabled.forEach(button=>button.disabled=true);
 status.textContent="";
 try{
  await post("/signals/"+encodeURIComponent(card.id)+"/"+action,
   {...body,expected_revision:card.revision});
  await refresh();
 }catch(error){
  if(error.status===409)await refresh({id:card.id,message:error.message});
  else status.textContent=error.message;
 }finally{
  enabled.forEach(button=>button.disabled=false);
 }
}

function renderSources(root,card,status){
 root.append(element("h4","Обезличенные источники"));
 if(!card.sources.some(source=>source.excerpt)){
  for(const example of card.examples)root.append(element("p",example,"evidence"));
 }
 for(const source of card.sources){
  const item=element("div",undefined,"evidence source");
  item.append(
   element("strong",source.ticket_id),
   element("small",(source.occurred_at||"Дата не указана")+(source.version?" · Версия "+source.version:""),"meta"),
   element("div",source.excerpt||"Фрагмент недоступен"),
  );
  if(source.version)item.append(ticketHistory(source.ticket_id));
  if(source.analysis_warning==="resolved_issue"){
   item.append(element("p","В тексте указано устранение сбоя. Проверьте актуальность источника.","meta"));
  }
  if(card.status==="pending_human_review"&&card.ticket_count>=2){
   const button=element("button","Это другая проблема","secondary");
   button.addEventListener("click",()=>{
    const reason=window.prompt("Основание исключения источника "+source.ticket_id);
    if(reason===null)return;
    const value=reason.trim();
    if(value.length<3||value.length>1000){
     status.textContent="Укажите основание от 3 до 1000 символов";
     return;
    }
    changeCard(card,"exclude",{ticket_id:source.ticket_id,reason:value},status,[button]);
   });
   item.append(button);
  }
  root.append(item);
 }
}

function ticketHistory(ticketId){
 const details=element("details");
 details.append(element("summary","История версий обращения "+ticketId));
 let loaded=false,loading=false;
 details.addEventListener("toggle",async()=>{
  if(!details.open||loaded||loading)return;
  loading=true;
  const output=element("div");
  details.append(output);
  try{
   const versions=await request("/tickets/"+encodeURIComponent(ticketId)+"/versions");
   for(const version of versions){
    const item=element("div",undefined,"evidence");
    item.append(element("small","Версия "+version.version+" · "+version.imported_at,"meta"),element("p",version.text));
    output.append(item);
   }
   loaded=true;
  }catch(error){output.append(element("p",error.message,"error"));}
  finally{loading=false;}
 });
 return details;
}

function changeExplanation(event){
 const reasons={resolved_issue:"Клиент сообщил об устранении сбоя",context_changed:"Источник больше не подтверждает прежнюю проблему",source_updated:"Обновлены сведения источника",source_added:"Источник подтверждает этот сигнал"};
 const lines=event.updates.map(u=>u.ticket_id+": версия "+u.from_version+" → "+u.to_version+". "+(reasons[u.reason]||"Изменены сведения"));
 lines.push("Активных источников стало "+event.sources_after+" вместо "+event.sources_before+".");
 if(event.added_ticket_ids.length)lines.push("Добавлены: "+event.added_ticket_ids.join(", ")+".");
 if(event.removed_ticket_ids.length)lines.push("Перестали подтверждать сигнал: "+event.removed_ticket_ids.join(", ")+".");
 return lines.join(" ");
}

function renderHistory(root,history){
 root.append(element("h4","История сигнала"));
 const list=element("ol",undefined,"timeline");
 for(const event of history){
  let line;
  if(event.type==="ticket_updated"){
   line=changeExplanation(event);
  }else if(event.type==="evidence_added"){
   line="Добавлены обращения: "+event.added_ticket_ids.join(", ");
  }else if(event.type==="source_excluded"){
   line="Исключен источник "+event.ticket_id+" · "+event.reason;
  }else if(event.type==="product_decision"){
   line="Решение владельца · "+(decisions[event.action]||event.action)+" · "+event.reason;
  }else{
   line=label(event.action)+" · "+event.reason;
  }
  if(event.actor)line+=" · "+event.actor;
  list.append(element("li",event.at+" · "+line));
 }
 root.append(list);
}

function reviewForm(card,status){
 const form=element("form",undefined,"actions");
 const reason=reasonInput("Основание решения аналитика");
 const buttons=element("div",undefined,"buttons");
 const approve=element("button","Подтвердить");
 const reject=element("button","Отклонить","reject");
 approve.value="approved";
 reject.value="rejected";
 const missingEvidence=card.sources.some(source=>source.excerpt===null);
 const resolvedEvidence=card.sources.some(source=>source.analysis_warning==="resolved_issue");
 approve.disabled=card.ticket_count<2||missingEvidence||resolvedEvidence;
 buttons.append(approve,reject);
 form.append(reason,buttons);
 if(card.ticket_count<2)form.append(element("small","Для подтверждения нужны хотя бы два источника.","meta"));
 if(missingEvidence)form.append(element("small","Повторите импорт или исключите источники без фрагментов.","meta"));
 if(resolvedEvidence)form.append(element("small","Перед подтверждением проверьте и исключите источники с указанием устранения сбоя.","meta"));
 form.addEventListener("submit",event=>{
  event.preventDefault();
  const value=reason.value.trim();
  if(value.length<3){reason.focus();return}
  changeCard(card,"review",{action:event.submitter.value,reason:value},status,[approve,reject]);
 });
 return form;
}

async function openDetail(id,notice){
 const load=++detailLoad;
 selected=id;
 renderList();
 const root=$("detail");
 const path="/signals/"+encodeURIComponent(id);
 root.replaceChildren(element("p","Загрузка доказательств…","empty"));
 try{
  const [card,history]=await Promise.all([request(path+"/card"),request(path+"/timeline")]);
  if(load!==detailLoad)return;
  const status=feedback();
  status.textContent=notice||"";
  root.replaceChildren(
   element("span",label(card.status),"tag "+card.status),
   element("h3",card.product+" · "+card.workflow),
   element("p",card.category+" · "+card.ticket_count+" обращений · "+card.method,"meta"),
   status,
  );
  if(card.reviewed_revision&&card.revision!==card.reviewed_revision){
   root.append(element("p","Доказательства изменились после подтверждения. Требуется повторная проверка. Ранее подтвержденный снимок доступен в очереди владельца.","evidence"));
  }
  if(card.latest_change){
   root.append(element("p",changeExplanation(card.latest_change),"evidence"));
   for(const id of card.latest_change.removed_ticket_ids)root.append(ticketHistory(id));
  }
  if(card.context){
   root.append(element("p","Признаки группировки · "+card.context.operation+" · "+card.context.symptom,"meta"));
  }
  renderSources(root,card,status);
  renderHistory(root,history);
  if(card.status==="pending_human_review")root.append(reviewForm(card,status));
 }catch(error){
  if(load===detailLoad)root.replaceChildren(element("p",error.message,"empty error"));
 }
}

function ownerForm(row){
 const form=element("form",undefined,"actions");
 const decision=element("select");
 decision.setAttribute("aria-label","Решение владельца");
 decision.required=true;
 const placeholder=element("option","Выберите решение");
 placeholder.value="";
 placeholder.disabled=true;
 placeholder.selected=true;
 decision.append(placeholder);
 for(const [value,name] of Object.entries(decisions)){
  const option=element("option",name);
  option.value=value;
  decision.append(option);
 }
 const reason=reasonInput("Основание решения владельца");
 const button=element("button","Сохранить решение");
 const status=feedback();
 form.append(decision,reason,button,status);
 form.addEventListener("submit",async event=>{
  event.preventDefault();
  const value=reason.value.trim();
  if(value.length<3){reason.focus();return}
  button.disabled=true;
  status.textContent="";
  try{
   await post("/product-queue/"+encodeURIComponent(row.signal_id)+"/decision",
    {decision:decision.value,reason:value});
   await refresh();
  }catch(error){
   if(error.status===409)await refresh();
   else status.textContent=error.message;
  }finally{
   button.disabled=false;
  }
 });
 return form;
}

function renderQueue(rows){
 const root=$("product-queue");
 root.replaceChildren();
 if(!rows.length){
  root.append(element("p","Пока нет подтвержденных карточек","empty"));
  return;
 }
 for(const row of rows){
  const entry=element("article",undefined,"queue-entry");
  entry.append(
   element("h3",row.product+" · "+row.workflow),
   element("p","Передано · "+row.delivered_at,"meta"),
   element("p",row.first_ticket_at?"Первое обращение · "+row.first_ticket_at:
    "Дата первого обращения недоступна","meta"),
  );
  if(row.decision){
   entry.append(
    element("p",(decisions[row.decision]||row.decision)+" · "+row.reason,"owner-decision"),
    element("p",[row.decided_at,row.decided_by].filter(Boolean).join(" · "),"meta"),
   );
  }
  if(row.snapshot){
   const details=element("details");
   details.append(element("summary","Карточка при передаче · источников "+row.snapshot.ticket_count));
   details.append(element("p",row.snapshot.category+" · "+row.snapshot.method,"meta"));
   renderSources(details,row.snapshot);
   if(!row.decision)details.append(ownerForm(row));
   entry.append(details);
  }else{
   entry.append(element("p","Историческая запись без снимка доказательств.","meta"));
  }
  root.append(entry);
 }
}

$("connect").addEventListener("click",()=>{token=$("token").value.trim();$("token").value="";refresh()});$("refresh").addEventListener("click",refresh);$("status").addEventListener("change",renderList);$("search").addEventListener("input",renderList);$("signals").replaceChildren(element("p","Введите токен аналитика для просмотра сигналов","empty"));

$("import-file").addEventListener("change",async event=>{
 const file=event.target.files[0];if(!file)return;
 if(file.size>1024*1024){$("import-result").textContent="Файл превышает 1 МБ";return}
 $("import-json").value=await file.text();
});
const analysisReasons={
 context_resolved:"Операция и результат определены",
 mixed_intents:"Несколько независимых задач — нужна проверка",
 ambiguous_workflow:"Не удалось выбрать один сценарий",
 missing_evidence:"Недостаточно исходных сведений",
 insufficient_context:"Недостаточно контекста",
 missing_workflow:"Сценарий не определен",
 missing_symptom:"Наблюдаемый результат не определен",
 ambiguous_symptom:"Описано несколько результатов",
 ambiguous_operation:"Описано несколько операций",
 missing_operation:"Операция не определена",
 declined_notification_request:"Нужно уточнить настройку нежелательных уведомлений",
 ambiguous_notification_need:"Нужно уточнить, когда отправлять уведомление",
 untrusted_instruction:"Обнаружены посторонние инструкции",
 individual_request:"Индивидуальное действие поддержки",
 guidance_requested:"Вопрос о порядке действий",
 resolved_issue:"В тексте указано устранение сбоя",
 informational:"Информационное сообщение",
};
function renderClassification(rows){
 const root=$("import-classified");
 root.replaceChildren();
 if(!rows?.length)return;
 const details=element("details");
 details.append(element("summary","Результат разбора · обращений "+rows.length));
 for(const row of rows){
  const item=element("div",undefined,"evidence");
  item.append(element("strong",row.id+" · "+row.product),
   element("p",row.category+" · "+row.workflow,"meta"),
   element("p",(row.needs_review?"Требуется проверка. ":"")+(analysisReasons[row.reason]||"Требуется проверка контекста")));
  details.append(item);
 }
 root.append(details);
}

$("import-submit").addEventListener("click",async()=>{
 const output=$("import-result"),button=$("import-submit");
 button.disabled=true;
 renderClassification([]);
 try{
  const tickets=JSON.parse($("import-json").value);
  if(!Array.isArray(tickets)||tickets.length<1||tickets.length>500)throw Error("Нужен массив из 1–500 обращений");
  const result=await post("/imports",{tickets});
  output.textContent="Загружено: "+result.processed+" · Новых обращений: "+result.staged_new+" · Обновленных обращений: "+(result.updated_tickets||0)+" · Всего проанализировано: "+result.analyzed_total+" · С указанием устранения сбоя: "+(result.resolved_tickets||0)+" · Сигналов: "+result.signals_found+" · Новых сигналов: "+result.new_signals;
  renderClassification(result.classified);
  await refresh();
 }catch(error){output.textContent="Ошибка импорта: "+error.message}
 finally{button.disabled=false}
});
