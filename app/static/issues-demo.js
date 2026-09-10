/* Synthetic, public fixture: no requests, no organisation data. */
const demoReport={
 'год':2026,'договоры':[{'код':'DEMO-НИР','название':'Демонстрационный договор','ФОТ':1200000,'выплачено':1000000,'остаток':200000}],
 'дефицит':[{'табельный':'DEMO-E1','фио':'Сотрудник А (пример)','месяц':3,'положено':100000,'выплачено':90000,'дефицит':10000,'причина':'Дефицит разрешён настройкой демонстрационного примера'}],
 'п4':[{'табельный':'DEMO-E2','фио':'Сотрудник Б (пример)','месяц':4,'итого':155000,'предел':150000,'запас':-5000,'отклонение':.033333}],
 'бэп':[{'договор':'DEMO-ГОЗ','месяц':5,'средняя':115000,'БЭП':110000,'запас':-5000,'отклонение':.04545}],
 'касса':[{'код':'DEMO-НИР','строки':{'конец':[100000,50000,-25000,0]}}],
 'трудоемкость':[{'договор':'DEMO-ГОЗ','строка':'Инженер · этап 2','план чел-мес':8,'факт чел-мес':6,'допуск':.05,'вне допуска':{'чел-мес':true},'статус':'не закрыто'}]
};
const demoRules=[{'правило':'Надбавка 122 — с того же договора, что и оклад','тип':'нарушать нельзя','состояние':'нарушено','раздел':'Выплаты','строки':[{'месяц':6,'объект':'Сотрудник В (пример)','что':'122 назначена с DEMO-НИР, оклад — с DEMO-ГОЗ'}]}];
const demoIssues=PlanIssues.collect(demoReport,demoRules,{warnings:[]},'demo');
let demoFilter={severity:'all',category:'all'}, savedScroll=0,savedId='';
const list=document.getElementById('demo-list'),detail=document.getElementById('demo-detail');
function renderDemo(){list.innerHTML=PlanIssues.render(demoIssues,demoFilter);}
const e=PlanIssues.esc;
function openDemo(id){
 const issue=demoIssues.find(x=>x.id===id);if(!issue)return;
 savedScroll=window.scrollY;savedId=id;
 list.hidden=true;detail.hidden=false;
 // Демонстрационная таблица показывает те же три графы, что и список.
 const cols=['Объект','Факт','Предел','Разница'];
 detail.innerHTML='<button type="button" class="review-open issue-back" id="demo-back">← К замечаниям</button><h2>Раздел '+issue.target.section+' · '+e(issue.title)+'</h2><p class="review-note">'+e(issue.period)+' · Демонстрационная таблица источника</p><div class="review-block"><table class="review-table"><thead><tr>'+cols.map(c=>'<th scope="col">'+e(c)+'</th>').join('')+'</tr></thead><tbody><tr class="demo-target" tabindex="-1"><th scope="row">'+e(issue.subject)+'</th>'+['fact','limit','diff'].map(k=>'<td class="amount '+(k==='diff'?'issue-target-cell':'')+'">'+e(PlanIssues.format(issue.metrics[k]))+'</td>').join('')+'</tr></tbody></table></div><p class="review-note">'+e(issue.detail)+'</p>';
 detail.querySelector('.demo-target').focus();window.scrollTo(0,0);
}
document.addEventListener('click',ev=>{
 const open=ev.target.closest('[data-issue-open]');if(open){openDemo(open.dataset.issueOpen);return;}
 const filter=ev.target.closest('[data-issue-filter]');if(filter){demoFilter.severity=filter.dataset.issueFilter;renderDemo();return;}
 if(ev.target.closest('#demo-back')){detail.hidden=true;list.hidden=false;window.scrollTo(0,savedScroll);const b=[...list.querySelectorAll('[data-issue-open]')].find(b=>b.dataset.issueOpen===savedId);if(b)b.focus({preventScroll:true});}
});
document.addEventListener('change',ev=>{if(ev.target.matches('[data-issue-category]')){demoFilter.category=ev.target.value;renderDemo();}});
renderDemo();
