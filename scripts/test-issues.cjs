const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
const issues=require('../app/static/issues.js');
const fixture=fs.readFileSync(require.resolve('../app/static/issues-demo.js'),'utf8').split('let demoFilter=')[0];
const sandbox={PlanIssues:issues};vm.createContext(sandbox);vm.runInContext(fixture+';this.fixture={report:demoReport,rules:demoRules,issues:demoIssues};',sandbox);
const data=sandbox.fixture;
// Шесть, а не семь: замечания «Кассовый разрыв» по кассе договора больше
// нет — касса отдельной величиной не показывается (SOURCE-009), а минус
// остатка ФОТ нарастающим итогом называет проверка rules.py (раздел 4).
assert.equal(data.issues.length,6);
assert.equal(new Set(data.issues.map(i=>i.id)).size,6);
assert.ok(!data.issues.some(i=>i.rule==='cash.negative'));
// У каждого замечания есть раздел назначения и три величины: значение,
// предел или норма, отклонение. Точная строка может отсутствовать у правила
// «в целом» — тогда переход открывает сам раздел.
for(const issue of data.issues){assert.ok(issue.target.section);
  assert.deepEqual(Object.keys(issue.metrics),['fact','limit','diff']);
  assert.ok(issue.id.startsWith('demo:'));}
assert.equal(data.issues.find(i=>i.rule==='pay.deficit').severity,'warning');
assert.equal(data.issues.find(i=>i.rule==='labor.чел-мес').severity,'warning');
// П4 — норма, а не потолок (правило 5): отклонение в любую сторону —
// нарушение «p4.deviation»; графа 12 раздела 9.2 — отклонение, ₽.
assert.equal(data.issues.find(i=>i.rule==='p4.deviation').target.month,4);
assert.equal(data.issues.find(i=>i.rule==='p4.deviation').target.column,12);
assert.equal(data.issues.find(i=>i.rule==='p4.deviation').severity,'error');
assert.equal(data.issues.find(i=>i.rule==='check.0').target.section,6);
assert.equal(data.issues.find(i=>i.rule==='check.0').target.row,null);
assert.equal(issues.collect({},null,{}).length,1);
assert.equal(issues.collect({},[{'состояние':'соблюдено'}],{}).length,0);
assert.equal(issues.collect({'п4':[{'итого':150000.4,'уровень':150000,'состояние':'Равно уровню П4'}]},[{'состояние':'соблюдено'}],{}).length,0);
const p4below=issues.collect({'п4':[{'фио':'А','месяц':2,'итого':140000,'уровень':150000,'отклонение':-10000,'состояние':'Ниже уровня П4'}]},[{'состояние':'соблюдено'}],{});
assert.equal(p4below.length,1);assert.equal(p4below[0].severity,'error');assert.equal(p4below[0].metrics.diff.value,-10000);
// БЭП (владелец продукта 30.09.2026): выше — нарушение, ниже — замечание,
// равно — замечаний нет.
const bep=issues.collect({'бэп':[
  {'договор':'Г','месяц':3,'средняя':113000,'БЭП':112261,'отклонение':739,'состояние':'Выше БЭП'},
  {'договор':'Г','месяц':4,'средняя':111261,'БЭП':112261,'отклонение':-1000,'состояние':'Ниже БЭП'},
  {'договор':'Г','месяц':5,'средняя':112261,'БЭП':112261,'отклонение':0,'состояние':'Равно БЭП'}]},[{'состояние':'соблюдено'}],{});
assert.deepEqual(bep.map(i=>[i.rule,i.severity,i.target.row,i.target.column]),
  [['bep.exceeded','error','bep:0',7],['bep.below','warning','bep:1',7]]);
assert.equal(bep.find(i=>i.rule==='bep.below').title,'Средняя ниже БЭП');
// Выплата приказом выше П2556 на штатную ставку (раздел 9.3) — замечание
// самого отчёта: проверки в rules.py у неё нет.
const orders=issues.collect({'п2556 приказ':[{'фио':'Б','месяц':6,'приказ':150000,'предел':140000,'запас':-10000,'состояние':'Выше предела'},
  {'фио':'В','месяц':6,'приказ':5000,'предел':140000,'запас':135000,'состояние':'Не выше предела'}]},[{'состояние':'соблюдено'}],{});
assert.deepEqual(orders.map(i=>[i.rule,i.target.section,i.target.row]),[['order2556.exceeded',9,'p2556o:0']]);
// Замечание проверки П2556 открывает строку 9.1 («p2556:N»): прежде раздел
// 9 искал строку только в таблице П4.
const p2556Rule=[{'правило':'П2556: оклад и 122 не выше предела по должности','название':'Приказ № 2556: оклад и надбавка 122 не выше предела на открытую ставку',
  'тип':'нарушать нельзя','состояние':'нарушено','единица':'₽','строки':[{'объект':'Иванов И. И., мар, C_A','факт':150000,'предел':140000,'нарушено':true}]}];
const p2556Report={'договоры':[{'код':'C_A'}],'ставки':[{'фио':'Иванов И. И.','табельный':'1001'}],
  'п2556':[{'табельный':'1001','договор':'C_A','месяц':1},{'табельный':'1001','договор':'C_A','месяц':3}]};
const p2556Issue=issues.collect(p2556Report,p2556Rule,{})[0];
assert.deepEqual([p2556Issue.target.section,p2556Issue.target.row],[9,'p2556:1']);
assert.equal(p2556Issue.title,'Приказ № 2556: оклад и надбавка 122 не выше предела на открытую ставку');
// Проверка остатка ФОТ нарастающим итогом открывает строку раздела 4.
const cumRule=[{'правило':'Деньги нельзя потратить раньше, чем они пришли','тип':'нарушать нельзя','состояние':'нарушено',
  'строки':[{'объект':'C_A, фев','что':'выплаты нарастающим итогом больше поступившего ФОТ на 10 ₽','нарушено':true}]}];
assert.equal(issues.collect({'договоры':[{'код':'C_A'}]},cumRule,{})[0].target.row,'cum:C_A:остаток');
// Нарушение, которое отчёт уже показал строкой (БЭП), проверка rules.py
// вторым замечанием не повторяет.
const bepRule=[{'правило':'БЭП: средняя зарплата по ГОЗ-договору в пределах базовой','тип':'нарушать нельзя','состояние':'нарушено',
  'строки':[{'объект':'Г, мар','факт':452000,'предел':449044,'нарушено':true}]}];
assert.equal(issues.collect({'бэп':[{'договор':'Г','месяц':3,'состояние':'Выше БЭП','отклонение':739}]},bepRule,{}).length,1);
const before=JSON.stringify(data.report);issues.collect(data.report,data.rules,{},'demo');assert.equal(JSON.stringify(data.report),before);
// Фильтров нет: показываются все замечания, ошибки первыми.
const html=issues.render(data.issues);
assert.ok(html.includes('П4'));assert.ok(html.includes('Недоплата заработной платы'));
assert.ok(!html.includes('issue-controls'));
// Графы формальные, части подписи — без знака-разделителя (UI-015).
assert.ok(html.includes('>Предел или норма<'));
assert.ok(!issues.render([{...data.issues[0],subject:'A',title:'T',period:'Март'}]).includes(' · '));
assert.ok(!issues.render([{...data.issues[0],subject:'<script>alert(1)</script>'}]).includes('<script>'));
const duplicate=issues.collect({'договоры':[{'код':'A','остаток':20,'ФОТ':100,'выплачено':80}]},[{'состояние':'соблюдено'}],{warnings:[['Предупреждение','Касса','A','Декабрь','Неосвоенный остаток на конец года']]});assert.equal(duplicate.length,1);
// Графы раздела 2.2: нераспределённый ФОТ — 6-я, выплаты по плану — 5-я.
assert.equal(duplicate[0].target.column,6);
console.log('Diagnostic contract tests passed: six categories, stable targets, severity, boundaries, escaping, immutability, deduplication.');

// Verify adapters point at rows emitted by the real report table builders.
const source=fs.readFileSync(require.resolve('../app/static/app.js'),'utf8');
assert.ok(!source.includes("note.className='issue-empty'"));
const grids=[];const report=JSON.parse(JSON.stringify(data.report));
Object.assign(report,{'кто':[],'незакрыто':[],
  'п2556':[{'табельный':'1001','фио':'А','договор':'C_A','месяц':3,'запас':-10000}],
  'п2556 приказ':[{'табельный':'1002','фио':'Б','месяц':6,'приказ':150000,'предел':140000,'запас':-10000,'состояние':'Выше предела'}]});
report['бэп'].push({'договор':'DEMO-ГОЗ','месяц':6,'средняя':110000,'БЭП':112261,'отклонение':-2261,'состояние':'Ниже БЭП'});
const ctx={esc:String,vrep:report,repMon:[3],MON:Array.from({length:12},(_,i)=>String(i+1)),repGoalsHtml:()=>'',secH:()=>'',subH:()=>'',chip:()=>'',rp:String,rpn:String,rf:String,rm:String,rs:String,nc:(v)=>v,gc:()=>'',mc:(v)=>v,monHead:()=>[],monGroupRow:()=>[],repFactTo:()=>+report['факт по']||0,repT:(h,r)=>{grids.push({h,r});return '';}};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function repLimits()'),source.indexOf('function closeFilterMenu()',source.indexOf('function repLimits()'))),ctx);
vm.runInContext('repLimits()',ctx);
const targets=issues.collect(report,data.rules,{},'demo').filter(i=>[8,9,10].includes(i.target.section));
assert.ok(targets.some(i=>i.rule==='bep.below')&&targets.some(i=>i.rule==='order2556.exceeded'));
for(const issue of targets){
 const grid=grids.find(g=>g.r.some(r=>r.reviewKey===issue.target.row));assert.ok(grid,issue.target.row);
 const row=grid.r.find(r=>r.reviewKey===issue.target.row);assert.ok(issue.target.column<=row.cells.length);
 // Номер графы — та графа, что называет отклонение или запас, а не соседняя.
 const head=grid.h[issue.target.column-1];
 assert.match(String(head&&head.t||head),/Отклонение|Запас|Состояние|Число исполнителей/,issue.rule);
}
console.log('Real report builders: БЭП, приказы № 2556 и № 4 and labour target rows verified.');

assert.equal(issues.collect({'дефицит':[{'дефицит':0}]},[{'состояние':'соблюдено'}],{}).length,0);
assert.equal(data.issues.find(i=>i.rule==='check.0').source,'rules');
