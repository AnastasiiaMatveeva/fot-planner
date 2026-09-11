const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
const issues=require('../app/static/issues.js');
const fixture=fs.readFileSync(require.resolve('../app/static/issues-demo.js'),'utf8').split('let demoFilter=')[0];
const sandbox={PlanIssues:issues};vm.createContext(sandbox);vm.runInContext(fixture+';this.fixture={report:demoReport,rules:demoRules,issues:demoIssues};',sandbox);
const data=sandbox.fixture;
assert.equal(data.issues.length,7);
assert.equal(new Set(data.issues.map(i=>i.id)).size,7);
// У каждого замечания есть раздел назначения и три величины: факт, предел,
// разница. Точная строка может отсутствовать у правила «в целом» — тогда
// переход открывает сам раздел.
for(const issue of data.issues){assert.ok(issue.target.section);
  assert.deepEqual(Object.keys(issue.metrics),['fact','limit','diff']);
  assert.ok(issue.id.startsWith('demo:'));}
assert.equal(data.issues.find(i=>i.rule==='pay.deficit').severity,'warning');
assert.equal(data.issues.find(i=>i.rule==='labor.чел-мес').severity,'warning');
assert.equal(data.issues.find(i=>i.rule==='p4.exceeded').target.month,4);
assert.equal(data.issues.find(i=>i.rule==='p4.exceeded').target.column,11);
assert.equal(data.issues.find(i=>i.rule==='check.0').target.section,6);
assert.equal(data.issues.find(i=>i.rule==='check.0').target.row,null);
assert.equal(data.issues.find(i=>i.rule==='cash.negative').target.column,3);
assert.equal(issues.collect({},null,{}).length,1);
assert.equal(issues.collect({},[{'состояние':'соблюдено'}],{}).length,0);
assert.equal(issues.collect({'п4':[{'отклонение':.0005}]},[{'состояние':'соблюдено'}],{}).length,0);
const before=JSON.stringify(data.report);issues.collect(data.report,data.rules,{},'demo');assert.equal(JSON.stringify(data.report),before);
// Фильтров нет: показываются все замечания, ошибки первыми.
const html=issues.render(data.issues);
assert.ok(html.includes('П4'));assert.ok(html.includes('Недоплата сотруднику'));
assert.ok(!html.includes('issue-controls'));
assert.ok(!issues.render([{...data.issues[0],subject:'<script>alert(1)</script>'}]).includes('<script>'));
const duplicate=issues.collect({'договоры':[{'код':'A','остаток':20,'ФОТ':100,'выплачено':80}]},[{'состояние':'соблюдено'}],{warnings:[['Предупреждение','Касса','A','Декабрь','Неосвоенный остаток на конец года']]});assert.equal(duplicate.length,1);
console.log('Diagnostic contract tests passed: seven categories, stable targets, severity, boundaries, escaping, immutability, deduplication.');

// Verify adapters point at rows emitted by the real report table builders.
const source=fs.readFileSync(require.resolve('../app/static/app.js'),'utf8');
assert.ok(!source.includes("note.className='issue-empty'"));
const grids=[];const report=JSON.parse(JSON.stringify(data.report));
Object.assign(report,{'кто':[],'незакрыто':[]});
const ctx={esc:String,vrep:report,repMon:[3],MON:Array.from({length:12},(_,i)=>String(i+1)),repGoalsHtml:()=>'',secH:()=>'',chip:()=>'',rp:String,rf:String,rm:String,rs:String,nc:(v)=>v,gc:()=>'',monHead:()=>[],monGroupRow:()=>[],repT:(h,r)=>{grids.push(r);return '';}};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function repLimits()'),source.indexOf('function closeFilterMenu()',source.indexOf('function repLimits()'))),ctx);
vm.runInContext('repLimits()',ctx);
for(const issue of data.issues.filter(i=>[8,9,10].includes(i.target.section))){
 const row=grids.flat().find(r=>r.reviewKey===issue.target.row);assert.ok(row,issue.target.row);assert.ok(issue.target.column<=row.cells.length);
}
console.log('Real report builders: BЭП, П4 and labour target rows verified.');

assert.equal(issues.collect({'дефицит':[{'дефицит':0}]},[{'состояние':'соблюдено'}],{}).length,0);
assert.equal(data.issues.find(i=>i.rule==='check.0').source,'rules');
