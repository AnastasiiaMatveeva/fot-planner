/* Shared diagnostics contract and renderer. No DOM access, requests or mutations. */
(function(root) {
  'use strict';
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const months = ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];
  const categories = {contracts:'Договоры',employees:'Сотрудники',labor:'Трудоёмкость',limits:'Ограничения',data:'Проверки данных'};
  const metric = (label,value,unit) => ({label,value,unit:unit || ''});
  /* Величины замечания: значение, предел или норма и отклонение между ними.
     Подписи графам даёт таблица, а не проверка, поэтому здесь только числа.
     Отклонение считается само: «превышение» и «недоплата» — это оно и есть. */
  const values = (fact,limit,unit,diff) => ({
    fact:{value:fact,unit:unit || ''},
    limit:{value:limit,unit:unit || ''},
    diff:{value:diff != null ? diff
            : (typeof fact === 'number' && typeof limit === 'number' ? fact - limit : null),
          unit:unit || ''}});
  const note = text => ({fact:{value:text,unit:''},limit:{value:null,unit:''},
                         diff:{value:null,unit:''}});
  const format = m => !m ? '—' : typeof m.value === 'number'
    ? m.value.toLocaleString('ru-RU',{maximumFractionDigits:2}) + (m.unit ? ' ' + m.unit : '')
    : String(m.value == null || m.value === '' ? '—' : m.value);
  /* Адрес замечания — раздел отчёта и строка в нём. Без адреса кнопка
     «Перейти» ведёт в общий раздел проверок, и экономист видит там чужую
     строку: «отклонение по трудоёмкости» открывалось на «нераспределённом
     ФОТ». Поэтому адрес считается для каждого источника замечаний, а
     раздел 15 остаётся только там, где место указать нечем.

     Ключи строк те же, что расставлены в отчёте (data-review-key):
     2 — шифр договора; 4 — «cum:шифр:остаток»; 6 — «pay:табельный:вид»
     (ищется по началу); 7 — «rate:табельный»; 8 — «bep:N»; 9 — «p2556:N»,
     «p4:N», «p2556o:N»; 10 — «labor:N»; 11 — «who:N»; 14 — «deficit:N».
     Номер графы (column) — порядковый номер графы таблицы без «№». */
  const RULE_SECTION = {
    'Человек получает всю месячную зарплату':6,
    'Оклад идет по ставке, а не произвольной суммой':6,
    'Надбавка 122 — с того же договора, что и оклад':6,
    'Режим «оклад плюс 152»':6,
    'П2556: оклад и 122 не выше предела по должности':9,
    'Платим только теми видами, которые разрешены договором':2,
    'Договор платит только в свои сроки':2,
    'Выплаты не превышают ФОТ договора':2,
    'ФОТ договоров освоен':2,
    'Деньги нельзя потратить раньше, чем они пришли':4,
    'БЭП: средняя зарплата по ГОЗ-договору в пределах базовой':8,
    'П4 при надбавке за интенсивность':9,
    'Суммарная ставка — не больше 1,5':7,
    'Совместительство — не больше 0,5 ставки':7,
    'Плановые человеко-месяцы закрыты':10,
    'Сумма трудоемкости выбрана':10,
    'Работу закрывает подходящая должность':11,
  };
  /* Раздел 9 — три таблицы: условие ищет строку в своей. Прежде раздел 9
     искал только в П4, и замечание по П2556 открывало раздел без строки. */
  const RULE_TABLE = {
    'П2556: оклад и 122 не выше предела по должности':'п2556',
    'П4 при надбавке за интенсивность':'п4',
  };
  /* Условия, которые отчёт уже показал своими строками (БЭП, П4,
     трудоёмкость): замечание проверки rules.py о том же было бы вторым
     замечанием об одном нарушении (UI-002). */
  const REPORT_COVERED = {
    'БЭП: средняя зарплата по ГОЗ-договору в пределах базовой':'bep.',
    'П4 при надбавке за интенсивность':'p4.',
    'Плановые человеко-месяцы закрыты':'labor.',
    'Сумма трудоемкости выбрана':'labor.',
  };
  const TABLE_KEY = {'п2556':'p2556','п4':'p4'};
  const MSG_SECTION = {'Трудоёмкость':10,'Дробление выплат':6,'Касса':4,
                       'Дефицит':14,'Конфликт':2,'Освоение':2};
  const ruleSection = name => RULE_SECTION[name] || null;

  /* Разобрать объект замечания: «Иванов Игорь Иванович, янв, C_BASE-26»,
     «C_GOZ-26, сен», «C_GOZ-26, Инженер», «C_GOZ-26 / Инженер / Петров». */
  function parseSubject(r, subject) {
    const text = String(subject == null ? '' : subject).trim();
    const parts = text.split(/\s*[,/]\s*/).filter(Boolean);
    const codes = new Set((r['договоры'] || []).map(c => c['код']));
    const people = (r['ставки'] || []).concat((r['помесячно'] || {})['сотрудники'] || []);
    const out = {contract:null, person:null, month:null, labor:-1, text};
    parts.forEach(part => {
      if (codes.has(part)) { out.contract = out.contract || part; return; }
      const mi = months.findIndex(m => m.toLowerCase().slice(0,3) === part.toLowerCase().slice(0,3));
      if (mi >= 0 && part.length <= 8) { out.month = out.month || mi + 1; return; }
      const who = people.find(x => x['фио'] === part || x['табельный'] === part);
      if (who) out.person = out.person || who['табельный'];
    });
    /* Договор и строку РКМ проверки пишут через запятую (UI-015); прежние
       записи — через косую черту. */
    out.labor = (r['трудоемкость'] || []).findIndex(l => [', ', ' / '].some(sep =>
      text === l['договор'] + sep + l['строка'] ||
      text.indexOf(l['договор'] + sep + l['строка']) === 0));
    return out;
  }

  /* Раздел плюс разобранный объект — в адрес строки. table — таблица
     раздела 9, в которой искать строку («п2556» или «п4»). */
  function place(r, section, subject, month, table) {
    const f = parseSubject(r, subject);
    const m = month || f.month || null;
    let row = null;
    if (section === 2 && f.contract) row = f.contract;
    else if (section === 4 && f.contract) row = 'cum:' + f.contract + ':остаток';
    else if (section === 6 && f.person) row = 'pay:' + f.person;
    else if (section === 7 && f.person) row = 'rate:' + f.person;
    else if (section === 8) {
      const i = (r['бэп'] || []).findIndex(b => (!f.contract || b['договор'] === f.contract)
        && (!m || b['месяц'] === m));
      if (i >= 0) row = 'bep:' + i;
    } else if (section === 9) {
      const key = table || 'п4';
      const i = (r[key] || []).findIndex(x => (!f.person || x['табельный'] === f.person)
        && (!m || x['месяц'] === m) && (!f.contract || !x['договор'] || x['договор'] === f.contract));
      if (i >= 0) row = TABLE_KEY[key] + ':' + i;
    } else if (section === 10 && f.labor >= 0) row = 'labor:' + f.labor;
    else if (section === 11 && f.labor >= 0) row = 'who:' + f.labor;
    else if (section === 14) {
      const i = (r['дефицит'] || []).findIndex(d => (!f.person || d['табельный'] === f.person)
        && (!m || d['месяц'] === m));
      if (i >= 0) row = 'deficit:' + i;
    }
    /* Раздел известен, а строка — нет (правило нарушено «в целом», без
       объекта): открываем раздел. Это всё равно то место, где проверяют
       факт, и лучше общего раздела проверок с чужой строкой. */
    return section ? {section, row, month:m} : null;
  }

  /* Состояние строки раздела 8 или 9.2. Его считает отчёт (report.py); у
     отчёта без него — по отклонению: доля у БЭП, рубли у П4. */
  const bepState = b => b['состояние'] || (b['отклонение'] > .0005 ? 'Выше БЭП'
    : b['отклонение'] < -.0005 ? 'Ниже БЭП' : 'Равно БЭП');
  const p4Level = b => b['уровень'] != null ? b['уровень'] : b['предел'];
  const p4State = b => {
    if (b['состояние']) return b['состояние'];
    const d = (b['итого'] || 0) - (p4Level(b) || 0);
    return d > 1 ? 'Выше уровня П4' : d < -1 ? 'Ниже уровня П4' : 'Равно уровню П4';
  };

  function collect(report, rules, result, runId) {
    const r = report || {}, out = [];
    function add(rule,category,severity,subject,title,metrics,target,detail) {
      const id = [runId || 'run',rule,target.row,target.month || '',target.column == null ? '' : target.column].join(':');
      if (out.some(x=>x.id === id)) return;
      out.push({id,rule,category,severity,subject,title,metrics,target,detail:detail || '',
        period:target.period || (target.month ? months[target.month-1] : target.section===15 ? 'Проверка версии' : 'За год'),
        source:rule.startsWith('check.')?'rules':rule.startsWith('message.')?'result':'report'});
    }
    /* Графы раздела 2.2: 5 — выплаты по плану за год, 6 — нераспределённый
       ФОТ на 31.12. */
    (r['договоры'] || []).forEach(c=>{
      if (c['остаток'] > .5) add('budget.remaining','contracts','warning',c['код'],'Нераспределённый ФОТ',
        values(c['выплачено'],c['ФОТ'],'₽'),{section:2,row:c['код'],column:6},c['название']);
      if (c['выплачено'] > c['ФОТ'] + .5) add('budget.exceeded','contracts','error',c['код'],'Выплаты превышают ФОТ договора',
        values(c['выплачено'],c['ФОТ'],'₽'),{section:2,row:c['код'],column:5});
    });
    (r['дефицит'] || []).forEach((d,i)=>{if(!(d['дефицит'] > .5)) return; add('pay.deficit','employees','warning',d['фио'] || d['табельный'],'Недоплата заработной платы',
      values(d['выплачено'],d['положено'],'₽'),{section:14,row:'deficit:'+i,column:6,month:d['месяц']},d['причина']);});
    /* Раздел 8: выше БЭП — нарушение, ниже — замечание: цель расчёта —
       ровно БЭП, недобор допустим, но нежелателен (владелец продукта
       30.09.2026). Графа 7 — отклонение средней от БЭП, ₽. */
    /* Месяц факта (правило 26) — «Факт»: показан, но не судится; замечания
       по нему нет ни здесь, ни в 9.2 и 9.3. */
    (r['бэп'] || []).forEach((b,i)=>{
      const st = bepState(b);
      if (st === 'Равно БЭП' || st === 'Факт') return;
      const above = st === 'Выше БЭП';
      add(above ? 'bep.exceeded' : 'bep.below','limits',above ? 'error' : 'warning',b['договор'],
        above ? 'Средняя выше БЭП' : 'Средняя ниже БЭП',
        values(b['средняя'],b['БЭП'],'₽',b['состояние'] ? b['отклонение'] : null),
        {section:8,row:'bep:'+i,column:7,month:b['месяц']});
    });
    /* Раздел 9.2: уровень П4 — норма (правило 5), отклонение в любую
       сторону сверх рубля — нарушение. Графа 12 — отклонение, ₽. */
    (r['п4'] || []).forEach((b,i)=>{
      const st = p4State(b);
      if (st === 'Равно уровню П4' || st === 'Факт') return;
      add('p4.deviation','limits','error',b['фио'] || b['табельный'],'Штатная часть не равна уровню П4',
        values(b['итого'],p4Level(b),'₽'),{section:9,row:'p4:'+i,column:12,month:b['месяц']});
    });
    /* Раздел 9.3: выплата приказом выше П2556 на штатную ставку. Проверки
       в rules.py у неё нет — замечание даёт сам отчёт. Графа 10 — запас. */
    (r['п2556 приказ'] || []).forEach((b,i)=>{
      if (b['состояние'] !== 'Выше предела') return;
      add('order2556.exceeded','limits','error',b['фио'] || b['табельный'],'Выплата приказом выше предела П2556',
        values(b['приказ'],b['предел'],'₽'),{section:9,row:'p2556o:'+i,column:10,month:b['месяц']});
    });
    /* Раздел 10: графы 5 — отклонение трудоёмкости, 9 — стоимости, 13 —
       средней стоимости, 16 — наибольшее число исполнителей, 17 — состояние. */
    (r['трудоемкость'] || []).forEach((l,i)=>{
      const flags=l['вне допуска'] || {}, row='labor:'+i;
      [['чел-мес','план чел-мес','факт чел-мес','чел.-мес.',5,'Трудоёмкость'],['сумма','план сумма','факт сумма','₽',9,'Стоимость трудоёмкости'],['средняя','средняя план','средняя факт','₽',13,'Средняя стоимость 1 чел.-мес.']].forEach(([key,p,f,u,col,title])=>{
        if (!flags[key]) return;
        add('labor.'+key,'labor','warning',l['договор']+', '+l['строка'],title+' вне допуска',
          values(l[f],l[p],u),{section:10,row,column:col});
      });
      if (flags['этап']) add('labor.stage','labor','error',l['договор']+', '+l['строка'],'Трудоёмкость вне месяцев этапа',
        note(l['статус']),{section:10,row,column:17});
      /* Предел числа исполнителей судится по месяцам плана: месяцы факта —
         история (правило 26). */
      const heads = l['людей макс план'] != null ? l['людей макс план'] : l['людей макс'];
      if (l['людей предел'] != null && heads > l['людей предел']) add('labor.headcount','labor','error',l['договор']+', '+l['строка'],'Превышено число исполнителей',
        values(heads,l['людей предел'],'чел.'),{section:10,row,column:16});
    });
    // Legacy independent checks remain visible. Their target is the exact check row,
    // not a guessed employee/month extracted from a human-readable description.
    (rules || []).forEach((rule,ri)=>{
      if (!['нарушено','внимание'].includes(rule['состояние'])) return;
      if (rule['правило']==='ФОТ договоров освоен' && out.some(x=>x.rule==='budget.remaining')) return;
      const covered = REPORT_COVERED[rule['правило']];
      if (covered && out.some(x=>x.rule.startsWith(covered))) return;
      const rows=rule['строки'] || [];
      const bad=rows.map((s,i)=>({s,i})).filter(x=>rule['состояние']==='внимание' || x.s['нарушено'] || x.s['что']);
      const chosen=bad.length ? bad : [{s:{'объект':rule['где'],'что':rule['факт']},i:-1}];
      chosen.forEach(({s,i})=>{
        const metrics=s['предел'] != null ? values(s['факт'],s['предел'],rule['единица'])
                                          : note(s['что'] || rule['факт']);
        const at = place(r, RULE_SECTION[rule['правило']], s['объект'], s['месяц'] || null, RULE_TABLE[rule['правило']])
                || {section:15,row:'rule:'+ri+':'+i,column:i<0?null:1,month:s['месяц'] || null};
        add('check.'+ri,'limits',rule['состояние']==='нарушено' && rule['тип']==='нарушать нельзя'?'error':'warning',s['объект'] || rule['раздел'],rule['название'] || rule['правило'],metrics,at,rule['смысл']);
      });
    });
    /* Одно сообщение решателя на каждый месяц — это одна проблема, а не
       десять: сводим по объекту и тексту, месяцы собираем в период. */
    const msgGroups=[], msgBy={};
    ((result || {}).warnings || []).forEach((w,i)=>{
      if (!Array.isArray(w) || !['Ошибка','Предупреждение'].includes(w[0])) return;
      if (/неосвоенный остаток/i.test(w[4] || '') && out.some(x=>x.rule==='budget.remaining' && x.subject===w[2])) return;
      const key=[w[0],w[1],w[2],w[4],w[5]].join('\u0001');
      if (!msgBy[key]) { msgBy[key]={i,w,months:[]}; msgGroups.push(msgBy[key]); }
      const m=months.indexOf(w[3]);
      if (m>=0 && msgBy[key].months.indexOf(m+1)<0) msgBy[key].months.push(m+1);
    });
    /* Сообщение о выплатах сотрудника показывать надо там, где его строки:
       в разделе 6. Ключ строки тот же, что у плана выплат, — «ФИО|вид». */
    const payKind=title=>{
      const m=/^(оклад|120|122|124|152|приказ)/i.exec(String(title||''));
      if (m) return m[1].toLowerCase();
      const k=/\b(120|122|124|152)\b/.exec(String(title||''));
      return k ? k[1] : null;
    };
    msgGroups.forEach(g=>{
      const w=g.w, list=g.months.sort((a,b)=>a-b);
      const period = !list.length ? null
        : list.length<=3 ? list.map(m=>months[m-1]).join(', ')
        : months[list[0]-1]+'–'+months[list[list.length-1]-1]+', '+list.length+' мес.';
      const num = typeof w[5]==='number' ? w[5] : parseFloat(String(w[5]||'').replace(/\s/g,'').replace(',','.'));
      const kind=payKind(w[4]);
      const person=(r['помесячно'] || {})['сотрудники'] || [];
      const who=person.find(p=>p['фио']===w[2] || p['табельный']===w[2]);
      /* Адрес сообщения решателя: раздел листа «Проблемы и предупреждения»
         переводится в раздел отчёта, объект — в строку. */
      const at = place(r, MSG_SECTION[w[1]], w[2], list[0] || null);
      const target = who && kind
        ? {section:6,row:'pay:'+who['табельный']+':'+kind,month:list[0] || null,period,column:null}
        : at ? Object.assign({period,column:null}, at)
        : {section:15,row:'message:'+g.i,month:list[0] || null,period};
      add('message.'+g.i,'data',w[0]==='Ошибка'?'error':'warning',w[2] || w[1],w[4] || 'Сообщение расчёта',
        isFinite(num) ? values(num,null) : note(w[1] || ''),target,w[6]);
    });
    if (!rules || !rules.length) add('checks.unavailable','data','warning','Версия расчёта','Проверки ограничений недоступны',
      note('соблюдение правил не подтверждено'),{section:15,row:'checks-unavailable'});
    return out.sort((a,b)=>(a.severity==='error'?0:1)-(b.severity==='error'?0:1));
  }
  // Замечаний у расчёта единицы, и все они нужны сразу: фильтры «Все /
  // Ошибки / Внимание» и «Область» прятали ровно то, ради чего экономист
  // открыл страницу. Ошибки идут первыми, порядок задаёт collect.
  // Графы названы формально (владелец продукта 30.09.2026): значение, предел
  // или норма и отклонение — у каждой строки своя единица при числе.
  function render(issues) {
    const rows=issues;
    return '<section class="issue-list" aria-label="Замечания к расчёту"><div class="review-section-title"><h3>Замечания к расчёту</h3><span class="review-count">'+issues.length+'</span></div>'+
      (rows.length ? '<div class="review-block"><table class="review-table issue-table"><thead><tr><th scope="col">Объект и период</th><th scope="col">Условие</th><th scope="col" class="num">Значение</th><th scope="col" class="num">Предел или норма</th><th scope="col" class="num">Отклонение</th><th scope="col">Действие</th></tr></thead><tbody>'+rows.map(x=>{
        const v=x.metrics;
        return '<tr data-issue-id="'+esc(x.id)+'"><td><strong'+(x.category==='contracts'?' class="code"':'')+'>'+esc(x.subject)+'</strong><small>'+esc(categories[x.category])+', '+esc(x.period)+'</small></td>'+
          '<td><span class="issue-severity '+x.severity+'">'+(x.severity==='error'?'Ошибка':'Внимание')+'</span><div class="issue-title">'+esc(x.title)+'</div></td>'+
          '<td class="num">'+esc(format(v.fact))+'</td><td class="num">'+esc(format(v.limit))+'</td>'+
          '<td class="num'+(typeof v.diff.value==='number'&&v.diff.value>0?' over':'')+'">'+esc(format(v.diff))+'</td>'+
          '<td><button type="button" class="review-open" data-issue-open="'+esc(x.id)+'" title="Открыть место замечания" aria-label="Перейти к замечанию: '+esc(x.subject)+', '+esc(x.title)+'">Перейти</button></td></tr>';
      }).join('')+'</tbody></table></div>' : '<div class="issue-empty">Замечаний нет</div>')+'</section>';
  }
  const api={collect,render,format,categories,esc,ruleSection};
  if(typeof module!=='undefined'&&module.exports) module.exports=api; else root.PlanIssues=api;
})(typeof globalThis!=='undefined'?globalThis:this);
