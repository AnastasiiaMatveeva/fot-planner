"""Сверка значений XLSX с кандидатом без вызова кода построения отчёта.

Контракт колонок намеренно независим от экспортёра: его ошибка или переименование
не должны автоматически менять ожидание аудитора. Покрытие ограничено ниже.
"""
from collections import Counter
from io import BytesIO
import json
from math import isfinite

from openpyxl import load_workbook

MONTHS = ('Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
          'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь')
KINDS = {1: 'оклад', 120: '120 надбавка (гостайна)', 122: '122 надбавка (качество)',
         124: '124 надбавка (интенсивность)', 152: '152 надбавка (доп. работа)',
         0: 'стимулирующая приказом'}


def _value(value):
    if value is None or value == '':
        return None
    if type(value) in (int, float):
        if not isfinite(value):
            raise ValueError('нечисловое значение в числовой ячейке')
        # XLSX читает 100.0 как int 100; тип Python не меняет сумму.
        rounded = round(value, 8)
        return ['number', format(rounded if rounded else 0, '.8f')]
    return [type(value).__name__, value]


def _key(values):
    return json.dumps([_value(v) for v in values], ensure_ascii=False, sort_keys=True)


def _table(book, name, columns, expected, *, required=True, sections=None):
    """Мультимножество сохраняет повторы; перестановка строк допустима."""
    record = {'sheet': name, 'columns': columns, 'expected_rows': len(expected),
              'numeric_decimal_places': 8, 'verdict': 'failed'}
    if name not in book.sheetnames:
        return {**record, 'verdict': 'failed' if required else 'passed',
                'actual_rows': 0, 'reason': 'лист отсутствует' if required else 'пустой необязательный лист отсутствует'}
    sheet = book[name]
    rows = [row for row in sheet.iter_rows() if any(c.value is not None for c in row)]
    if not rows:
        return {**record, 'actual_rows': 0, 'verdict': 'failed' if expected else 'passed',
                'reason': 'пустой лист'}
    headers = [c.value for c in rows[0]]
    if any(headers.count(c) != 1 for c in columns):
        return {**record, 'reason': 'нет обязательной колонки или повтор заголовка'}
    positions = [headers.index(c) for c in columns]
    observed = []
    for row in rows[1:]:
        cells = [row[i] for i in positions]
        if sections is not None and cells[0].value not in sections:
            continue
        if any(c.data_type == 'f' for c in cells):
            return {**record, 'reason': 'формула вместо значения',
                    'cells': [c.coordinate for c in cells if c.data_type == 'f']}
        observed.append([c.value for c in cells])
    want, got = Counter(_key(r) for r in expected), Counter(_key(r) for r in observed)
    missing, extra = want - got, got - want
    return {**record, 'actual_rows': len(observed),
            'missing_rows': sum(missing.values()), 'unexpected_rows': sum(extra.values()),
            'verdict': 'failed' if missing or extra else 'passed',
            'reason': 'строки не соответствуют кандидату' if missing or extra else ''}


def _cash_rows(context, candidate):
    rows = []
    def display(value):
        return round(value, 2) if abs(value) > .005 else None
    for c in context.contracts:
        balances = {b.month: b for b in candidate.contract_balances if b.contract_id == c.id}
        payments = {m: [a for a in candidate.allocations
                       if a.contract_id == c.id and a.month == m and a.amount > .005]
                    for m in range(1, 13)}
        for section, metrics in (
            ('Поступления и выплаты', ('Поступление', 'Выплаты', 'Остаток на конец',
                                      'Количество сотрудников', 'Количество выплат')),
            ('Остатки', ('Остаток на начало', 'Поступление', 'Выплаты', 'Остаток на конец',
                        'Минимальный остаток', 'Доступно сверх минимума'))):
            for metric in metrics:
                if metric == 'Минимальный остаток' and not any(
                        b.min_balance_required > .005 for b in balances.values()):
                    continue
                values = []
                for month in range(1, 13):
                    b = balances.get(month)
                    if metric == 'Количество сотрудников':
                        value = len({a.employee_id for a in payments[month]})
                    elif metric == 'Количество выплат':
                        value = len(payments[month])
                    elif b is None:
                        value = 0
                    elif metric == 'Доступно сверх минимума':
                        value = max(0, b.closing_balance - b.min_balance_required)
                    else:
                        attr = {'Остаток на начало': 'opening_balance', 'Поступление': 'inflow',
                                'Выплаты': 'spent', 'Остаток на конец': 'closing_balance',
                                'Минимальный остаток': 'min_balance_required'}[metric]
                        value = getattr(b, attr)
                        if metric == 'Минимальный остаток' and value <= .005:
                            value = 0
                    values.append(float(value))
                rows.append([section, c.id, c.name, metric,
                             *[display(v) for v in values], display(sum(values))])
    if not context.contracts:
        rows = [['Поступления и выплаты', 'Нет договоров', None, None, *[None] * 13],
                ['Остатки', 'Нет данных по остаткам', None, None, *[None] * 13]]
    return rows


def audit_export(raw, context, candidate):
    employees = {e.id: e for e in context.employees}
    contracts = {c.id: c for c in context.contracts}
    plans = []
    for a in candidate.allocations:
        if a.amount <= .005:
            continue
        e, c = employees.get(a.employee_id), contracts.get(a.contract_id)
        plans.append([a.employee_id, e.full_name if e else '', e.position if e else '',
                      e.equivalence_group if e else '', a.year, MONTHS[a.month - 1],
                      a.contract_id, c.name if c else '', KINDS[int(a.payment_kind)],
                      round(a.amount, 2), a.source, 'да' if a.is_manual else 'нет'])
    has_deficit = any(d.amount > .005 for d in candidate.deficits)
    deficits = [[d.employee_id, employees[d.employee_id].full_name if d.employee_id in employees
                 else d.employee_id, MONTHS[d.month - 1], round(d.due_amount, 2),
                 round(d.paid_amount, 2), round(d.amount, 2)]
                for d in candidate.deficits] if has_deficit else []
    rates = [[a.employee_id, a.contract_id, a.month, a.open_rate,
              'да' if a.is_main else 'нет', a.position or ''] for a in candidate.open_rate_attributions]
    book = load_workbook(BytesIO(raw), read_only=True, data_only=False)
    try:
        checks = [
            _table(book, 'План выплат', ['код строки', 'ФИО', 'должность', 'окладная группа должности',
                'год', 'месяц', 'договор', 'название договора', 'вид выплаты', 'сумма',
                'источник решения', 'зафиксировано'], plans),
            _table(book, 'Дефициты', ['код строки', 'ФИО', 'месяц', 'требовалось выплатить',
                'выплачено', 'дефицит'], deficits, required=has_deficit),
            _table(book, 'открытые_ставки', ['код строки', 'договор', 'месяц', 'ставка',
                'основное', 'должность'], rates, required=bool(rates)),
            _table(book, 'цели', ['цель', 'значение', 'единица', 'приоритет', 'вес',
                'нормировано', 'вклад'], [[g.name, g.value, g.unit, g.priority, g.weight,
                    g.normalized, g.contribution] for g in candidate.goals], required=bool(candidate.goals)),
            _table(book, 'ФОТ по договорам', ['раздел', 'договор', 'название договора', 'показатель',
                'Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн', 'Июл', 'Авг', 'Сен', 'Окт', 'Ноя', 'Дек',
                'Итого'], _cash_rows(context, candidate), sections={'Поступления и выплаты', 'Остатки'}),
        ]
    finally:
        book.close()
    return {'schema': 1, 'scope': 'export-core-tables-v1',
            'verdict': 'passed' if all(c['verdict'] == 'passed' for c in checks) else 'failed',
            'checks': checks,
            'limitations': ['проверены перечисленные колонки пяти таблиц; освоение план-факт, трудоёмкость, ШР, сводка, тексты статусов и оформление не проверены',
                            'значения сравниваются с кандидатом; его экономическую допустимость проверяет result_audit']}
