# -*- coding: utf-8 -*-
"""Основание плана: какие договоры и какие версии документов идут в расчёт.

Реестр один на организацию: договор живёт несколько лет, документы по нему
общие для всех планов. А варианты плана считают разное: «без гранта» рядом с
«с грантом», расчёт по прежней редакции РКМ, пока новая не подписана. Поэтому
выбор живёт у плана, а не в реестре (SOURCE-003):

* договор участвует в расчёте плана, пока флажок у него не снят;
* у документа по умолчанию действует последняя версия, но план может считать
  по прежней — её строки берутся из снимка, записанного при замене версии;
* условие договора, которого нет в документах — разрешённые выплаты, срок,
  право на ставку, крайние сроки выплат — план может перебить для себя
  (SOURCE-006). Реквизиты договора не переопределяются: это его свойства.

Реестр при этом не переписывается: выбор применяется к копиям строк при
сборке входа, и соседний план видит реестр таким, какой он есть.
"""
from __future__ import annotations

import json

from sqlalchemy import func

import actuality
from db import (Contract, ContractValue, Document, Employee, Inflow, LaborRow,
                SalaryMonth, SalaryRow, SecretAllowance, Substitution, record_decision)

#: Строки реестра, у которых есть документ-источник.
ROW_MODELS = (Employee, Contract, LaborRow, Inflow, SecretAllowance, Substitution,
              ContractValue, SalaryRow, SalaryMonth)

#: Счётчики результата обработки — те же слова, что в карточке документа.
COUNT_NAMES = {"employees": "сотрудников", "contracts": "договоров",
               "labor_rows": "строк трудоемкости", "inflows": "поступлений",
               "secret_allowances": "надбавок 120", "substitutions": "правил замещения",
               "contract_values": "величин пакета", "salary_rows": "строк зарплаты",
               "salary_months": "сумм зарплаты"}

#: Ключ данных сборки входа → модель строк.
DATA_KEYS = (("employees", Employee), ("contracts", Contract), ("labor", LaborRow),
             ("inflows", Inflow), ("secret", SecretAllowance), ("substitutions", Substitution),
             ("values", ContractValue))

#: Строки, которые делают документ документом договора: сам договор, его
#: трудоёмкость, касса и величины пакета. Надбавка 120 ссылается на договор
#: секретности, но живёт в штатном расписании — к договору штатку она не
#: привязывает.
LINK_MODELS = ((Contract, "code"), (LaborRow, "contract_code"), (Inflow, "contract_code"),
               (ContractValue, "contract_code"))

#: Группы каталога, кроме договоров: у каждой своя вкладка реестра.
GROUPS = ("staff", "salary", "facts", "reference", "substitutions", "unlinked")

#: Что план переопределяет у договора (SOURCE-006): подпись → графа модели.
#: Реквизитов здесь нет намеренно. Шифр, наименование, номер, вид, счёт, ГОЗ,
#: подразделение, фонд и приоритет — свойства договора: два плана не могут
#: считать один договор с разным номером, а приоритет владелец продукта отнёс
#: к свойствам 16.09.2026. Список общий, поэтому добавить поле — одна строка.
#: Графы договора, которые экономист вводит руками (SOURCE-007): в документах
#: пакета их нет — РКМ и структура цены дают трудоёмкость, ФОТ, срок и
#: нормативы, а счёт, ГОЗ, приоритет, выплаты и ставки нет. Шифр опознаёт
#: договор, наименование правится на его экране — их здесь нет.
MANUAL = {"номер": "number", "вид": "kind", "подразделение": "department",
          "счёт": "account", "ГОЗ": "goz", "фонд": "fund",
          "начало": "date_from", "окончание": "date_to",
          "разрешенные выплаты": "kinds", "приоритет": "priority",
          "основное": "allow_main", "совместительство": "allow_part_time",
          "оклад до": "salary_deadline", "надбавки до": "allowance_deadline"}
#: Ручной ввод хранится величиной договора без документа: строки документа при
#: повторном разборе стираются и создаются заново, а такая величина остаётся,
#: и ручное значение возвращается поверх (reapply_manual).
MANUAL_FIELD = "ручной ввод: "
_YESNO = ("goz", "allow_main", "allow_part_time")


def _normalize(column, text):
    """Значение графы из текста поля: сумма числом, да/нет словом, пусто — None."""
    text = (text or "").strip()
    if not text:
        return None
    if column == "fund":
        try:
            return float(text.replace(" ", "").replace(" ", "").replace(",", "."))
        except ValueError:
            return None
    if column in _YESNO:
        return "да" if text.lower() in ("да", "yes", "true", "1", "+") else "нет"
    return text[:200]


def set_manual(db, contract, fields):
    """Графы договора, введённые экономистом (SOURCE-007).

    Значение идёт в строку договора и в память ручного ввода. Пустое значение
    снимает ручной ввод: до следующего разбора графа пустая, после — такая,
    какой её даст документ. Возвращает {графа: {было, стало}} по изменённым.
    """
    changed = {}
    for column, raw in (fields or {}).items():
        if column not in MANUAL.values():
            continue
        text = "" if raw is None else str(raw)
        value = _normalize(column, text)
        was = getattr(contract, column)
        if column == "fund":
            same = ((was is None and value is None) or
                    (was is not None and value is not None and abs(float(was) - value) < 0.005))
        else:
            same = (was or None) == (value or None)
        if same:
            continue
        setattr(contract, column, value)
        row = (db.query(ContractValue)
               .filter_by(contract_code=contract.code, field=MANUAL_FIELD + column)
               .filter(ContractValue.document_id.is_(None)).first())
        if value is None:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(ContractValue(contract_code=contract.code, field=MANUAL_FIELD + column,
                                 text=text.strip()[:120], source="введено экономистом"))
        else:
            row.text = text.strip()[:120]
        changed[column] = {"было": was, "стало": value}
    return changed


def manual_columns(db):
    """Какие графы у каких договоров введены руками: {шифр: [графа]}."""
    out = {}
    for r in (db.query(ContractValue).filter(ContractValue.field.like(MANUAL_FIELD + "%"))
              .filter(ContractValue.document_id.is_(None)).all()):
        out.setdefault(r.contract_code, []).append(r.field[len(MANUAL_FIELD):])
    return out


def reapply_manual(db):
    """Вернуть ручной ввод поверх строк договоров после разбора документа.

    Разбор стирает строки документа и создаёт заново — с тем, что в
    документе. Введённое экономистом документом не отменяется.
    """
    by_code = {}
    for r in (db.query(ContractValue).filter(ContractValue.field.like(MANUAL_FIELD + "%"))
              .filter(ContractValue.document_id.is_(None)).all()):
        by_code.setdefault(r.contract_code, {})[r.field[len(MANUAL_FIELD):]] = r.text
    n = 0
    if not by_code:
        return n
    for c in db.query(Contract).all():
        for column, text in (by_code.get(c.code) or {}).items():
            if column in MANUAL.values():
                setattr(c, column, _normalize(column, text))
                n += 1
    return n


#: Порядок здесь — порядок на экране: сначала срок, потом что платить, потом
#: до какого числа, потом можно ли открыть ставку. По алфавиту «надбавки до»
#: вставало перед «началом», и связанные поля расходились по списку.
OVERRIDE = {"начало": "date_from", "окончание": "date_to",
            "разрешенные выплаты": "kinds",
            "оклад до": "salary_deadline", "надбавки до": "allowance_deadline",
            "основное": "allow_main", "совместительство": "allow_part_time",
            # Месяцы, когда с договора в этом плане не платят (01.10.2026):
            # графы в реестре нет — условие только плана, «1, 3, 4».
            "месяцы без выплат": "closed_months"}

#: Что план переопределяет у строки штатного расписания (решение пользователя
#: 30.09.2026: «для нового плана возможно они захотят менять штатное
#: расписание и с разными зарплатами пересчитывать»): ставку и срок строки.
#: Зарплата по месяцам — отдельным словарём «зарплата» {месяц: сумма}.
#: Реестр общий: правка живёт у плана, соседний план её не видит.
#: Должность — с 30.09.2026 (пользователь: «тут добавь возможность вручную
#: менять должность»): от неё зависят категория, БЭП и пределы приказов.
EMP_OVERRIDE = {"ставка": "rate", "начало": "date_from", "окончание": "date_to",
                "должность": "position"}


# ── выбор плана ─────────────────────────────────────────────────
def read(case):
    """Выбор плана: снятые договоры и выбранные версии {первая версия: выбранная}.

    Сборку входа зовут и со стендов с планом-заглушкой без графы — у такого
    плана выбора нет.
    """
    raw = getattr(case, "plan_basis", None)
    try:
        got = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        got = {}
    if not isinstance(got, dict):
        got = {}
    off = {str(c).strip() for c in (got.get("вне расчёта") or []) if str(c).strip()}
    versions = {}
    for root, doc_id in (got.get("версии") or {}).items():
        try:
            versions[int(root)] = int(doc_id)
        except (TypeError, ValueError):
            continue
    # Переопределения читаются через OVERRIDE: поле, выпавшее из списка,
    # молча перестаёт действовать, а не уходит в решатель мимо правила.
    overrides = {}
    for code, fields in (got.get("договоры") or {}).items():
        if not isinstance(fields, dict):
            continue
        kept = {k: v for k, v in fields.items() if k in OVERRIDE}
        if kept:
            overrides[str(code).strip()] = kept
    # Сотрудники — так же через перечень полей; месяц зарплаты — число 1–12.
    staff = {}
    for code, fields in (got.get("сотрудники") or {}).items():
        if not isinstance(fields, dict):
            continue
        kept = {k: v for k, v in fields.items() if k in EMP_OVERRIDE}
        pay = {}
        for month, amount in (fields.get("зарплата") or {}).items():
            try:
                m, a = int(month), float(amount)
            except (TypeError, ValueError):
                continue
            if 1 <= m <= 12:
                pay[m] = a
        if pay:
            kept["зарплата"] = pay
        # Значения реестра на момент правки (синхронизация с реестром,
        # 30.09.2026): реестр потом меняется, и экран показывает, что правка
        # сделана поверх прежнего значения.
        reg = fields.get("реестр")
        if kept and isinstance(reg, dict):
            kept["реестр"] = {str(k): v for k, v in reg.items()}
        if kept:
            staff[str(code).strip()] = kept
    # Снятые строки штатного расписания — перечнем снятых, а не взятых, как у
    # договоров: строка, добавленная в реестр позже, попадает в расчёт сама.
    staff_off = {str(c).strip() for c in (got.get("сотрудники вне расчёта") or [])
                 if str(c).strip()}
    # Значения договора в реестре на момент правки условия (30.09.2026):
    # отдельным словарём, а не внутри правки — число правок договора экран
    # считает по её полям.
    base = {}
    for code, fields in (got.get("условия реестра") or {}).items():
        if isinstance(fields, dict) and str(code).strip() in overrides:
            kept = {k: v for k, v in fields.items() if k in overrides[str(code).strip()]}
            if kept:
                base[str(code).strip()] = kept
    return {"вне расчёта": off, "версии": versions, "договоры": overrides,
            "сотрудники": staff, "сотрудники вне расчёта": staff_off,
            "условия реестра": base}


def _write(case, basis):
    case.plan_basis = json.dumps(
        {"вне расчёта": sorted(basis["вне расчёта"]),
         "версии": {str(k): v for k, v in sorted(basis["версии"].items())},
         "договоры": {k: basis["договоры"][k] for k in sorted(basis["договоры"])},
         "сотрудники": {k: _staff_json(basis["сотрудники"][k])
                        for k in sorted(basis.get("сотрудники") or {})},
         "сотрудники вне расчёта": sorted(basis.get("сотрудники вне расчёта") or ()),
         "условия реестра": {k: basis["условия реестра"][k]
                             for k in sorted(basis.get("условия реестра") or {})}},
        ensure_ascii=False)


def _staff_json(fields):
    """Правка строки для хранения: месяцы зарплаты — строками по порядку."""
    out = {k: v for k, v in fields.items() if k not in ("зарплата", "реестр")}
    if fields.get("реестр"):
        out["реестр"] = {str(k): fields["реестр"][k] for k in sorted(fields["реестр"])}
    if fields.get("зарплата"):
        out["зарплата"] = {str(m): a for m, a in sorted(fields["зарплата"].items())}
    return out


def _decide(db, case, basis, action, grounds):
    from fot_planner.harness_local import decisions as D
    record_decision(db, kind="состав плана", subject_kind="план", subject_id=case.id,
                    subject_digest=D.digest(sorted(basis["вне расчёта"]),
                                            sorted(basis["версии"].items()),
                                            sorted(basis["договоры"].items()),
                                            sorted((k, _staff_json(v)) for k, v in
                                                   (basis.get("сотрудники") or {}).items()),
                                            sorted(basis.get("сотрудники вне расчёта") or ()),
                                            sorted((basis.get("условия реестра") or {}).items())),
                    scope="план %d" % case.id, action=action, grounds=grounds)


def set_included(db, case, codes, included, grounds):
    """Флажок договоров в реестре. False — выбор не изменился."""
    basis = read(case)
    codes = {str(c).strip() for c in codes or [] if str(c).strip()}
    before = set(basis["вне расчёта"])
    if included:
        basis["вне расчёта"] -= codes
    else:
        basis["вне расчёта"] |= codes
    changed = sorted(before ^ basis["вне расчёта"])
    if not changed:
        return False
    _write(case, basis)
    _decide(db, case, basis, {"договоры": changed, "в расчёте": bool(included)}, grounds)
    one = len(changed) == 1
    actuality.mark_review(
        db, actuality.runs_of_case(db, case.id),
        "%s %s %s" % ("договор" if one else "договоры", ", ".join(changed),
                      ("возвращён в расчёт" if one else "возвращены в расчёт") if included
                      else ("снят с расчёта" if one else "сняты с расчёта")))
    db.commit()
    return True


def set_staff_included(db, case, codes, included, grounds):
    """Флажок строк штатного расписания в этом плане (пользователь
    30.09.2026: «добавь опцию выбора галочками нужных сотрудников, вдруг
    уволить захотят кого-то»). False — выбор не изменился.

    Как у договоров: снятая строка остаётся в реестре и в соседних планах, а
    из входа этого плана уходит вместе с надбавкой 120, зарплатой месяцев и
    закреплениями (apply, apply_salary, main._apply_plan_settings).
    """
    basis = read(case)
    codes = {str(c).strip() for c in codes or [] if str(c).strip()}
    before = set(basis["сотрудники вне расчёта"])
    if included:
        basis["сотрудники вне расчёта"] -= codes
    else:
        basis["сотрудники вне расчёта"] |= codes
    changed = sorted(before ^ basis["сотрудники вне расчёта"])
    if not changed:
        return False
    _write(case, basis)
    _decide(db, case, basis, {"сотрудники": changed, "в расчёте": bool(included)}, grounds)
    names = dict(db.query(Employee.code, Employee.fio).filter(Employee.code.in_(changed)).all())
    one = len(changed) == 1
    actuality.mark_review(
        db, actuality.runs_of_case(db, case.id),
        "%s %s %s" % ("сотрудник" if one else "сотрудники",
                      ", ".join(names.get(c) or c for c in changed),
                      ("возвращён в расчёт" if one else "возвращены в расчёт") if included
                      else ("снят с расчёта" if one else "сняты с расчёта")))
    db.commit()
    return True


def _day(text):
    """Дата «дд.мм.гггг» или «гггг-мм-дд» → (год, месяц, день); иначе None."""
    import re
    t = str(text or "").strip()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})", t)
    if m:
        return int(m.group(3)), int(m.group(2)), int(m.group(1))
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def _same(name, plan, registry):
    """Правка плана и значение реестра — одно и то же: ставка — числом,
    даты — день в день, виды выплат — набором, да/нет — словом."""
    import re
    a = str(plan if plan is not None else "").strip()
    b = str(registry if registry is not None else "").strip()
    if not a or not b:
        return False
    if name == "ставка":
        try:
            return abs(float(a.replace(",", ".")) - float(b.replace(",", "."))) < 1e-6
        except ValueError:
            return False
    if name in ("начало", "окончание", "оклад до", "надбавки до"):
        return _day(a) is not None and _day(a) == _day(b)
    if name == "разрешенные выплаты":
        split = lambda t: {x.strip().lower() for x in re.split(r"[,;]", t) if x.strip()}  # noqa: E731
        return split(a) == split(b)
    if name in ("основное", "совместительство"):
        yes = lambda t: t.lower() in ("да", "true", "1", "+")  # noqa: E731
        return yes(a) == yes(b)
    return a.lower() == b.lower()


def dissolve_caught_up(db, case):
    """Правки плана, с которыми реестр сравнялся, снимаются сами
    (пользователь 30.09.2026: «реестр сравнялся с правкой плана — снимать
    правку автоматически»). Обычно так и бывает: план предложил ставку 0,5,
    приказ подписали, новая версия штатного расписания принесла 0,5 — правке
    больше нечего делать, и дальше план идёт за реестром.

    Расчёт от этого не меняется — значение то же, поэтому к пересмотру
    ничего не помечается; снятое пишется в журнал решений. Возвращает
    перечень снятого: ["E1: ставка", "C_A: окончание", …]."""
    if case is None or getattr(case, "id", None) is None:
        return []
    basis = read(case)
    removed = []
    staff = basis["сотрудники"]
    if staff:
        emps = {e.code: e for e in db.query(Employee).filter(Employee.code.in_(list(staff))).all()}
        # Зарплата месяца — сумма строк человека (управленческие
        # подразделения, 01.10.2026).
        import salary as _salary
        paid = _salary.month_totals(db, getattr(case, "year", None), staff)
        words = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
                 "сентябрь", "октябрь", "ноябрь", "декабрь")
        for code in list(staff):
            fields = dict(staff[code])
            reg = dict(fields.pop("реестр", None) or {})
            e = emps.get(code)
            if e is not None:
                current = {"ставка": e.rate, "начало": e.date_from, "окончание": e.date_to,
                           "должность": e.position}
                for name in EMP_OVERRIDE:
                    if name in fields and _same(name, fields[name], current[name]):
                        fields.pop(name)
                        reg.pop(name, None)
                        removed.append("%s: %s" % (code, name))
            pay = dict(fields.pop("зарплата", None) or {})
            for m in sorted(pay):
                cur = paid.get((code, m))
                if cur is not None and abs(float(cur) - float(pay[m])) < 0.005:
                    pay.pop(m)
                    reg.pop("м%d" % m, None)
                    removed.append("%s: зарплата, %s" % (code, words[m - 1]))
            if pay:
                fields["зарплата"] = pay
            if fields:
                if reg:
                    fields["реестр"] = reg
                staff[code] = fields
            else:
                staff.pop(code)
    ctr = basis["договоры"]
    if ctr:
        rows = {c.code: c for c in db.query(Contract).filter(Contract.code.in_(list(ctr))).all()}
        for code in list(ctr):
            c = rows.get(code)
            if c is None:
                continue
            now = dict(ctr[code])
            for name in list(now):
                if name in OVERRIDE and _same(name, now[name], getattr(c, OVERRIDE[name], None)):
                    now.pop(name)
                    removed.append("%s: %s" % (code, name))
            snap = {k: v for k, v in (basis["условия реестра"].get(code) or {}).items() if k in now}
            if now:
                ctr[code] = now
            else:
                ctr.pop(code)
            if snap:
                basis["условия реестра"][code] = snap
            else:
                basis["условия реестра"].pop(code, None)
    if not removed:
        return []
    _write(case, basis)
    _decide(db, case, basis, {"сняты правки: реестр сравнялся": removed},
            "реестр сравнялся с правкой плана")
    db.commit()
    return removed


def set_overrides(db, case, code, fields, grounds):
    """Условия договора, заданные этим планом (SOURCE-006). False — без перемен.

    Пустое значение снимает переопределение, а не пишет пустоту: иначе
    «вернуть как в договоре» пришлось бы делать, перепечатывая базовое
    значение руками и рискуя разойтись с ним на пробел.
    """
    basis = read(case)
    code = str(code or "").strip()
    if not code:
        return False
    before = dict(basis["договоры"].get(code) or {})
    now = dict(before)
    # Что сейчас в договоре — запоминается при правке: изменится договор в
    # реестре — экран скажет об этом (синхронизация с реестром, 30.09.2026).
    snap = dict(basis["условия реестра"].get(code) or {})
    row = db.query(Contract).filter_by(code=code).first()
    for name, value in (fields or {}).items():
        if name not in OVERRIDE:
            continue
        text = "" if value is None else str(value).strip()
        if text:
            now[name] = text[:200]
            if row is not None:
                was = getattr(row, OVERRIDE[name], None)
                snap.setdefault(name, "" if was is None else str(was))
        else:
            now.pop(name, None)
            snap.pop(name, None)
    if now == before:
        return False
    if now:
        basis["договоры"][code] = now
    else:
        basis["договоры"].pop(code, None)
    snap = {k: v for k, v in snap.items() if k in now}
    if snap:
        basis["условия реестра"][code] = snap
    else:
        basis["условия реестра"].pop(code, None)
    _write(case, basis)
    changed = sorted(set(before) ^ set(now)
                     | {k for k in set(before) & set(now) if before[k] != now[k]})
    _decide(db, case, basis, {"договор": code, "переопределено": changed, "значения": now},
            grounds)
    actuality.mark_review(db, actuality.runs_of_case(db, case.id),
                          "у договора %s изменены условия плана: %s"
                          % (code, ", ".join(changed)))
    db.commit()
    return True


def _staff_value(name, text):
    """Правка строки штатного расписания, приведённая к одному виду, или
    ValueError со словами, что не так: ставка — число больше 0 и не больше 2,
    срок — дата «дд.мм.гггг», должность — текст без лишних пробелов."""
    if name == "должность":
        import re as _re
        return _re.sub(r"\s+", " ", text).strip()
    if name == "ставка":
        try:
            rate = float(text.replace(" ", "").replace(",", "."))
        except ValueError:
            raise ValueError("ставка «%s» — не число" % text)
        if not 0 < rate <= 2:
            raise ValueError("ставка %s вне пределов: больше 0 и не больше 2" % text)
        return ("%.4f" % rate).rstrip("0").rstrip(".").replace(".", ",")
    import re
    import datetime as _dt
    if not re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", text):
        raise ValueError("%s «%s» — не дата дд.мм.гггг" % (name, text))
    d, m, y = (int(x) for x in text.split("."))
    try:
        _dt.date(y, m, d)
    except ValueError:
        raise ValueError("%s «%s» — такой даты нет" % (name, text))
    return "%02d.%02d.%04d" % (d, m, y)


def set_employee_overrides(db, case, code, fields, grounds):
    """Строка штатного расписания в этом плане (30.09.2026): ставка, срок и
    зарплата по месяцам поверх реестровых. False — без перемен.

    Пустое значение снимает правку, а не пишет пустоту — как у условий
    договора (SOURCE-006): «вернуть как в штатном расписании» не требует
    перепечатывать реестровое значение. Неверное значение — ValueError со
    словами, что не так.
    """
    basis = read(case)
    code = str(code or "").strip()
    if not code:
        return False
    staff = basis["сотрудники"]
    before = staff.get(code) or {}
    now = {k: v for k, v in before.items() if k not in ("зарплата", "реестр")}
    pay = dict(before.get("зарплата") or {})
    # Что сейчас в реестре — запоминается при правке: если реестр потом
    # изменится, экран скажет «в реестре изменилось», а не молча оставит
    # правку поверх нового значения (синхронизация с реестром, 30.09.2026).
    reg = dict(before.get("реестр") or {})
    emp = db.query(Employee).filter_by(code=code).first()
    registry = {"ставка": ("%.4f" % emp.rate).rstrip("0").rstrip(".").replace(".", ",")
                if emp is not None and emp.rate is not None else "",
                "начало": (emp.date_from or "") if emp is not None else "",
                "окончание": (emp.date_to or "") if emp is not None else "",
                "должность": (emp.position or "") if emp is not None else ""}
    year = getattr(case, "year", None)
    import salary as _salary
    paid = {m: a for (_c, m), a in _salary.month_totals(db, year, [code]).items()}
    for name, value in (fields or {}).items():
        if name == "зарплата" and isinstance(value, dict):
            for month, amount in value.items():
                try:
                    m = int(month)
                except (TypeError, ValueError):
                    continue
                if not 1 <= m <= 12:
                    continue
                text = "" if amount is None else str(amount).strip()
                if not text:
                    pay.pop(m, None)
                    reg.pop("м%d" % m, None)
                    continue
                reg.setdefault("м%d" % m, paid.get(m))
                try:
                    a = float(text.replace(" ", "").replace("\u00a0", "").replace(",", "."))
                except ValueError:
                    raise ValueError("сумма «%s» — не число" % text)
                if a < 0:
                    raise ValueError("сумма %s меньше нуля" % text)
                pay[m] = round(a, 2)
            continue
        if name not in EMP_OVERRIDE:
            continue
        text = "" if value is None else str(value).strip()
        if text:
            now[name] = _staff_value(name, text)
            reg.setdefault(name, registry[name])
        else:
            now.pop(name, None)
            reg.pop(name, None)
    if pay:
        now["зарплата"] = pay
    if now and reg:
        now["реестр"] = reg
    if now == before:
        return False
    if now:
        staff[code] = now
    else:
        staff.pop(code, None)
    _write(case, basis)
    changed = sorted(k for k in set(before) | set(now) if before.get(k) != now.get(k))
    _decide(db, case, basis, {"сотрудник": code, "переопределено": changed,
                              "значения": _staff_json(now)}, grounds)
    actuality.mark_review(db, actuality.runs_of_case(db, case.id),
                          "у сотрудника %s изменены условия плана: %s"
                          % (code, ", ".join(changed)))
    db.commit()
    return True


def apply_salary(case, months, warn=None):
    """Зарплата по месяцам с правками этого плана: [(код, месяц, сумма)].

    Правка месяца заменяет сумму из таблицы «Заработная плата по месяцам» или
    добавляет её, если суммы не было; прочие месяцы — как в реестре. Каждая
    правка называется в допущениях."""
    basis = read(case) if case is not None else {}
    staff = basis.get("сотрудники") or {}
    # Строка вне расчёта плана в расчёт не идёт — и суммы её месяцев тоже.
    gone = basis.get("сотрудники вне расчёта") or set()
    if gone:
        months = [(c, m, a) for c, m, a in months if str(c) not in gone]
    if not any(f.get("зарплата") for c, f in staff.items() if c not in gone):
        return months
    by = {(c, m): a for c, m, a in months}
    said = []
    for code in sorted(staff):
        if code in gone:
            continue
        for m, a in sorted((staff[code].get("зарплата") or {}).items()):
            was = by.get((code, m))
            by[(code, m)] = a
            said.append("%s, %s — %s вместо %s" % (code, m, _number(a),
                                                    _number(was) if was is not None else "пусто"))
    if warn is not None and said:
        warn.append("Зарплата по месяцам в этом плане: %s." % "; ".join(said))
    return sorted((c, m, a) for (c, m), a in by.items())


def set_version(db, case, root, doc_id, grounds):
    """Версия документа, по которой считает план. False — выбор не изменился.

    Последняя версия не записывается: план без записи идёт за последней, и
    новая загрузка сама станет его версией. Прежняя — только со снимком строк.
    """
    items = chains(db).get(int(root))
    if not items:
        raise LookupError("документ не найден")
    target = next((d for d in items if d.id == int(doc_id)), None)
    if target is None:
        raise LookupError("это версия другого документа")
    top = head(items)
    if target.id != top.id and _snapshot(target) is None:
        raise ValueError("строки этой версии не сохранены — выбрать её нельзя")
    basis = read(case)
    before = basis["версии"].get(int(root))
    if target.id == top.id:
        basis["версии"].pop(int(root), None)
    else:
        basis["версии"][int(root)] = target.id
    if basis["версии"].get(int(root)) == before:
        return False
    _write(case, basis)
    _decide(db, case, basis, {"документ": top.name, "версия": target.version or 1,
                              "id": target.id}, grounds)
    actuality.mark_review(db, actuality.runs_of_case(db, case.id),
                          "документ «%s» — в расчёте версия %s"
                          % (top.name, target.version or 1))
    db.commit()
    return True


# ── версии и снимки ─────────────────────────────────────────────
def _columns(model):
    return [c.name for c in model.__table__.columns if c.name != "id"]


def snapshot(db, doc):
    """Строки реестра этой версии — JSON для графы документа.

    Пишется, когда версию заменяют новой: строки уходят из реестра, а план,
    который решит считать по этой версии, возьмёт их отсюда.
    """
    rows = {}
    for model in ROW_MODELS:
        got = db.query(model).filter_by(document_id=doc.id).order_by(model.id).all()
        if got:
            rows[model.__tablename__] = [{n: getattr(r, n) for n in _columns(model)} for r in got]
    return json.dumps({"состояние": doc.state, "строки": rows}, ensure_ascii=False, default=str)


def _snapshot(doc):
    raw = getattr(doc, "rows_snapshot", None)
    if not raw:
        return None
    try:
        got = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def snapshot_rows(doc):
    """Строки снимка моделями вне сессии: {таблица: [строки]}; None — снимка нет.

    Объекты не добавляются в сессию, поэтому в базу не попадут, даже если
    сессию сборки потом закоммитят.
    """
    snap = _snapshot(doc)
    if snap is None:
        return None
    by_table = {m.__tablename__: m for m in ROW_MODELS}
    out = {}
    for table, rows in (snap.get("строки") or {}).items():
        model = by_table.get(table)
        if model is None:
            continue
        names = set(_columns(model))
        out[table] = [model(**{k: v for k, v in r.items() if k in names}) for r in rows]
    return out


def restore(db, doc):
    """Вернуть в реестр строки версии из её снимка. None — снимка нет."""
    rows = snapshot_rows(doc)
    if rows is None:
        return None
    n = 0
    for items in rows.values():
        for r in items:
            db.add(r)
            n += 1
    doc.state = (_snapshot(doc) or {}).get("состояние") or "разобран"
    doc.rows_snapshot = None
    return n


def _root(doc, by_id):
    seen, cur = set(), doc
    while cur.supersedes_id in by_id and cur.id not in seen:
        seen.add(cur.id)
        cur = by_id[cur.supersedes_id]
    return cur.id


def chains(db):
    """Документы реестра цепочками версий: {первая версия: [версии, новая первой]}.

    Цепочка без действующей версии — история документа, удалённого раньше,
    чем появились снимки: строк для расчёта у неё нет, в каталог она не идёт.
    Песочница плана — не реестр.
    """
    docs = [d for d in db.query(Document).order_by(Document.id).all()
            if (d.scope or "реестр") != "план"]
    by_id = {d.id: d for d in docs}
    grouped = {}
    for d in docs:
        grouped.setdefault(_root(d, by_id), []).append(d)
    out = {}
    for root, items in grouped.items():
        if any(d.state != "заменен" for d in items):
            out[root] = sorted(items, key=lambda d: (d.version or 1, d.id), reverse=True)
    return out


def head(items):
    """Действующая версия цепочки."""
    return next((d for d in items if d.state != "заменен"), items[0])


def used_version(basis, root, items):
    """Версия, по которой считает план: выбранная со снимком, иначе последняя."""
    top = head(items)
    pick = basis["версии"].get(root)
    if pick and pick != top.id:
        for d in items:
            if d.id == pick and _snapshot(d) is not None:
                return d
    return top


def links_of(db, doc):
    """Договоры, которым документ дал строки."""
    out = set()
    for model, field in LINK_MODELS:
        for (code,) in (db.query(getattr(model, field))
                        .filter(model.document_id == doc.id).distinct()):
            if code:
                out.add(str(code))
    return out


# ── сборка входа ────────────────────────────────────────────────
def _codes(text):
    return [x.strip() for x in str(text or "").replace(",", ";").split(";") if x.strip()]


def closed_months(case):
    """{шифр: [месяцы 1–12]} — месяцы без выплат договоров этого плана
    (владелец продукта 01.10.2026: галочка снимает договор на весь срок, а
    нужен «конкретный месяц»)."""
    out = {}
    for code, fields in read(case)["договоры"].items():
        months = set()
        for part in str(fields.get("месяцы без выплат") or "").replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= 12:
                months.add(int(part))
        if months:
            out[code] = sorted(months)
    return out


def _copy(row):
    """Копия строки вне сессии: правка для одного плана не должна уйти в реестр."""
    model = type(row)
    return model(**{n: getattr(row, n) for n in _columns(model)})


def apply(db, case, rows, warn=None):
    """Выбор плана и пакет договора поверх строк реестра. rows — {ключ сборки: [строки]}.

    Возвращает такой же словарь; строки реестра не меняются: где плану нужна
    другая строка, в словарь идёт копия.
    """
    basis = read(case)
    warn = warn if warn is not None else []
    out = {k: list(v) for k, v in rows.items()}

    if basis["версии"]:
        all_chains = chains(db)
        for root in sorted(basis["версии"]):
            items = all_chains.get(root)
            if not items:
                continue
            top, chosen = head(items), used_version(basis, root, items)
            if chosen.id == top.id:
                continue
            snap = snapshot_rows(chosen) or {}
            for key, model in DATA_KEYS:
                if key in out:
                    out[key] = ([r for r in out[key] if r.document_id != top.id]
                                + snap.get(model.__tablename__, []))
            warn.append("По документу «%s» в расчёте версия %s, последняя — %s."
                        % (top.name, chosen.version or 1, top.version or 1))

    if "labor" in out:
        _one_labor_source(out, warn)
    off = set(basis["вне расчёта"])
    if off:
        warn.append("Договоры вне расчёта этого плана: %s." % ", ".join(sorted(off)))
        if "contracts" in out:
            out["contracts"] = [c for c in out["contracts"] if str(c.code) not in off]
    # Переопределения — до заполнения из пакета: поле, заданное планом, уже не
    # пустое, и пакет его не перекрывает (SOURCE-004 заполняет только пустые).
    if basis["договоры"] and "contracts" in out:
        out["contracts"] = [_override(c, basis["договоры"].get(str(c.code)), warn)
                            for c in out["contracts"]]
    if basis["сотрудники"] and "employees" in out:
        out["employees"] = [_override_employee(e, basis["сотрудники"].get(str(e.code)), warn)
                            for e in out["employees"]]
    if "employees" in out:
        # Строка ушла из реестра (уволен, новая версия штатного расписания):
        # правка плана ей больше не к чему — называем, а не прячем.
        present = {str(e.code) for e in out["employees"]}
        lost = sorted((set(basis["сотрудники"]) | basis["сотрудники вне расчёта"]) - present)
        if lost:
            warn.append("Правки плана по строкам, которых нет в штатном расписании реестра: "
                        "%s — не учтены." % ", ".join(lost))
    gone = basis["сотрудники вне расчёта"]
    if gone and "employees" in out:
        names = sorted({str(e.fio or e.code) for e in out["employees"] if str(e.code) in gone})
        out["employees"] = [e for e in out["employees"] if str(e.code) not in gone]
        if "secret" in out:
            out["secret"] = [r for r in out["secret"] if str(r.employee_code) not in gone]
        if names:
            warn.append("Сотрудники вне расчёта этого плана: %s." % ", ".join(names))
    empty = _fill_from_package(out, getattr(case, "year", None), warn)
    if off or empty:
        _exclude(out, off | empty, warn)
    return out


def _override(contract, fields, warn):
    """Копия договора с условиями этого плана поверх реестровых (SOURCE-006).

    Копия, а не правка строки: реестр общий, и соседний план обязан видеть
    договор таким, какой он есть. Каждое изменение называется в допущениях —
    молчаливое переопределение неотличимо от данных документа.
    """
    if not fields:
        return contract
    copy, said = _copy(contract), []
    for name in sorted(fields):
        if name == "месяцы без выплат":
            # Не графа договора: запреты по месяцам строит сборка входа
            # (main._apply_plan_settings), о них — своё допущение.
            continue
        value, column = fields[name], OVERRIDE[name]
        was = getattr(copy, column, None)
        if str(was if was is not None else "") == str(value):
            continue
        setattr(copy, column, value)
        said.append("%s — «%s» вместо «%s»"
                    % (name, value, was if was not in (None, "") else "не задано"))
    if said:
        warn.append("Договор %s считается по условиям этого плана: %s."
                    % (copy.code, "; ".join(said)))
    return copy


def _override_employee(emp, fields, warn):
    """Копия строки штатного расписания со ставкой и сроком этого плана.

    Копия, а не правка строки: штатное расписание общее. Зарплата месяцев —
    не поле строки, её подставляет apply_salary."""
    fields = {k: v for k, v in (fields or {}).items() if k in EMP_OVERRIDE}
    if not fields:
        return emp
    copy, said = _copy(emp), []
    for name in sorted(fields):
        value, column = fields[name], EMP_OVERRIDE[name]
        was = getattr(copy, column, None)
        new = float(str(value).replace(",", ".")) if name == "ставка" else value
        if was is not None and str(was) == str(new):
            continue
        setattr(copy, column, new)
        said.append("%s — «%s» вместо «%s»"
                    % (name, value, was if was not in (None, "") else "не задано"))
    if said:
        warn.append("Сотрудник %s считается по условиям этого плана: %s."
                    % (copy.code, "; ".join(said)))
    return copy


def _exclude(out, off, warn):
    """Убрать договоры из входа: сам договор, его трудоёмкость, кассу, 120 и
    упоминания в разрешённых и запрещённых договорах сотрудников."""
    out["contracts"] = [c for c in out.get("contracts", []) if str(c.code) not in off]
    out["labor"] = [r for r in out.get("labor", []) if str(r.contract_code) not in off]
    out["inflows"] = [r for r in out.get("inflows", []) if str(r.contract_code) not in off]
    if "values" in out:
        out["values"] = [v for v in out["values"] if str(v.contract_code) not in off]
    lost = [r for r in out.get("secret", []) if str(r.secret_contract_code or "") in off]
    if lost:
        warn.append("Надбавка 120 не учтена у %s: договор секретности вне расчёта."
                    % ", ".join(sorted({str(r.employee_code) for r in lost})))
    out["secret"] = [r for r in out.get("secret", [])
                     if str(r.secret_contract_code or "") not in off]
    left = sorted({str(c.code) for c in out["contracts"]})
    people, stranded = [], []
    for e in out.get("employees", []):
        allowed, forbidden = _codes(e.allowed_contracts), _codes(e.forbidden_contracts)
        if not (set(allowed) | set(forbidden)) & off:
            people.append(e)
            continue
        copy = _copy(e)
        kept = [c for c in allowed if c not in off]
        copy.allowed_contracts = "; ".join(kept) or None
        copy.forbidden_contracts = "; ".join(c for c in forbidden if c not in off) or None
        if allowed and not kept:
            # Пустой перечень разрешённых значит «можно всюду»: сотрудник
            # единственного снятого договора получил бы все остальные.
            copy.forbidden_contracts = "; ".join(left) or None
            stranded.append(str(e.fio or e.code))
        people.append(copy)
    out["employees"] = people
    if stranded:
        warn.append("%s разрешены только на договорах вне расчёта — остальные "
                    "договоры им запрещены." % ", ".join(stranded))


def _latest_labor(rows):
    """Строки трудоёмкости, которые идут в расчёт: на договор и год — строки
    одного документа, последнего загруженного. Возвращает (строки, отброшенные
    документы по (договор, год))."""
    groups = {}
    for r in rows:
        groups.setdefault((str(r.contract_code), r.year), {}).setdefault(
            r.document_id or 0, []).append(r)
    keep, dropped = set(), {}
    for key, by_doc in groups.items():
        last = max(by_doc)
        keep.update(id(r) for r in by_doc[last])
        if len(by_doc) > 1:
            dropped[key] = (by_doc[last], [rs for d, rs in sorted(by_doc.items()) if d != last])
    return [r for r in rows if id(r) in keep], dropped


def _one_labor_source(out, warn):
    """Трудоёмкость договора на год — из одного документа пакета.

    РКМ и структура цены описывают одних и тех же людей: сложить их строки
    значит удвоить план. Если год дают несколько документов, берутся строки
    последнего загруженного, остальные называются в допущениях.
    """
    out["labor"], dropped = _latest_labor(out["labor"])
    for (code, year), (kept, others) in sorted(dropped.items(), key=lambda x: (x[0][0], x[0][1] or 0)):
        names = sorted({r.source or "документ" for rs in others for r in rs})
        warn.append("Трудоёмкость договора %s на %s год есть в нескольких документах пакета; "
                    "взята из «%s», не учтены: %s."
                    % (code, year or "каждый", kept[0].source or "документ",
                       ", ".join("«%s»" % n for n in names)))


def _money(v):
    return "{:,.0f}".format(v).replace(",", " ")


def _fill_from_package(out, year, warn):
    """Пустые срок и ФОТ договора — из пакета (SOURCE-004). Возвращает шифры
    договоров, у которых ФОТ года так и не нашёлся.

    Заданное в реестре пакет не переписывает: расхождение называется. ФОТ
    года — основная зарплата периодов этого года; если её дают несколько
    документов пакета, берётся последний загруженный. Договор без ФОТ в
    расчёт не идёт: пустой объект, созданный под пакет, иначе испортил бы
    вход всем планам.
    """
    if "contracts" not in out:
        return set()
    by_code = {}
    for v in out.get("values") or []:
        by_code.setdefault(str(v.contract_code), []).append(v)
    kept, empty = [], set()
    for c in out["contracts"]:
        code, vals, copy = str(c.code), by_code.get(str(c.code), []), None
        start = next((v for v in vals if v.field == "срок: начало" and v.text), None)
        end = next((v for v in vals if v.field == "срок: окончание" and v.text), None)
        if start and end:
            if not (c.date_from and c.date_to):
                copy = _copy(c)
                copy.date_from = copy.date_from or start.text
                copy.date_to = copy.date_to or end.text
                warn.append("Срок договора %s — из пакета: %s–%s («%s», %s)."
                            % (code, copy.date_from, copy.date_to, start.source or "документ",
                               start.label or "срок"))
            elif (c.date_from, c.date_to) != (start.text, end.text):
                warn.append("Срок договора %s: в реестре %s–%s, в пакете %s–%s («%s»); "
                            "в расчёте — реестр." % (code, c.date_from, c.date_to, start.text,
                                                     end.text, start.source or "документ"))
        by_doc = {}
        for v in vals:
            if v.field == "основная заработная плата" and year and v.year == year and v.value:
                by_doc.setdefault(v.document_id or 0, []).append(v)
        if by_doc:
            last = max(by_doc)
            fund = round(sum(v.value for v in by_doc[last]), 2)
            src = by_doc[last][0].source or "документ"
            others = [round(sum(v.value for v in vs), 2) for d, vs in by_doc.items() if d != last]
            if any(abs(o - fund) > 1 for o in others):
                warn.append("ФОТ договора %s на %s год в документах пакета расходится (%s ₽); "
                            "взят последний загруженный «%s»."
                            % (code, year, " / ".join(_money(x) for x in others + [fund]), src))
            if c.fund is None:
                copy = copy or _copy(c)
                copy.fund = fund
                warn.append("ФОТ договора %s на %s год — из пакета: %s ₽, основная заработная "
                            "плата периодов года («%s»)." % (code, year, _money(fund), src))
            elif abs(c.fund - fund) > 1:
                warn.append("ФОТ договора %s: в реестре %s ₽, в пакете на %s год %s ₽ («%s»); "
                            "в расчёте — реестр." % (code, _money(c.fund), year, _money(fund), src))
        final = copy or c
        if final.fund is None:
            warn.append("Договор %s: ФОТ на %s год нет ни в реестре, ни в пакете — в расчёт "
                        "не взят." % (code, year or "плановый"))
            empty.add(code)
            continue
        kept.append(final)
    out["contracts"] = kept
    return empty


def row_sources(rows):
    """Сколько строк каждого документа ушло во вход: {документ: {что: сколько}}."""
    counts = {}
    for key, model in DATA_KEYS:
        name = COUNT_NAMES[model.__tablename__]
        for r in rows.get(key) or []:
            doc_id = getattr(r, "document_id", None)
            if doc_id:
                one = counts.setdefault(doc_id, {})
                one[name] = one.get(name, 0) + 1
    return counts


# ── каталог реестра ─────────────────────────────────────────────
def _dt(v):
    return v.strftime("%d.%m.%Y %H:%M") if v else None


def _json(raw):
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _place(doc, made):
    """Вкладка реестра для документа без договора."""
    kind = (doc.kind or "").strip().lower()
    if made.get("сотрудников") or kind == "штатное расписание":
        return "staff"
    if made.get("строк зарплаты") or kind == "зарплата по месяцам":
        return "salary"
    # Факт начислений (30.09.2026) — своя вкладка реестра. Строки факта в
    # счётчики версий не входят: новый файл заменяет факт своих месяцев, а
    # не прежний документ целиком (app/facts.py).
    if kind == "факт начислений":
        return "facts"
    if made.get("правил замещения") or kind == "правила замещения должностей":
        return "substitutions"
    if doc.gave or kind == "нормативный документ":
        return "reference"
    return "unlinked"


def catalog(db, case):
    """Документы реестра по договорам и вкладкам, с версиями и выбором плана.

    Документ договора — загруженный в договор или давший ему строки. Общий
    файл («Договоры 2026.xlsx» на пять договоров) стоит в каталоге каждого из
    них: версия у него одна на всех, и это видно по перечню договоров.
    """
    # Реестр без плана (план 0): выбора нет, но форма основания та же самая —
    # отдельный словарь здесь однажды отстал от read() на ключ «договоры», и
    # библиотека отвечала 500.
    basis = read(case)
    live_links, live_counts = {}, {}
    for model, field in LINK_MODELS:
        for doc_id, code in db.query(model.document_id, getattr(model, field)).distinct():
            if doc_id and code:
                live_links.setdefault(doc_id, set()).add(str(code))
    for model in ROW_MODELS:
        name = COUNT_NAMES[model.__tablename__]
        for doc_id, n in (db.query(model.document_id, func.count(model.id))
                          .group_by(model.document_id)):
            if doc_id:
                live_counts.setdefault(doc_id, {})[name] = n
    known = sorted({str(c) for (c,) in db.query(Contract.code).distinct() if c})
    contracts = {code: [] for code in known}
    groups = {g: [] for g in GROUPS}

    for root, items in sorted(chains(db).items()):
        top = head(items)
        chosen = used_version(basis, root, items)
        codes, versions = set(), []
        for d in items:
            if d.state != "заменен":
                made = dict(live_counts.get(d.id, {}))
                codes |= live_links.get(d.id, set())
            else:
                snap = _snapshot(d) or {}
                made = {COUNT_NAMES[t]: len(r) for t, r in (snap.get("строки") or {}).items()
                        if t in COUNT_NAMES and r}
                for model, field in LINK_MODELS:
                    codes |= {str(r[field]) for r in (snap.get("строки") or {})
                              .get(model.__tablename__, []) if r.get(field)}
            if d.contract_code:
                codes.add(d.contract_code)
            versions.append({"id": d.id, "name": d.name, "kind": d.kind, "state": d.state,
                             "version": d.version or 1, "uploaded": _dt(d.uploaded),
                             "produced": made, "gave": _json(d.gave),
                             "selectable": d.id == top.id or _snapshot(d) is not None})
        entry = {"root": root, "name": top.name, "used": chosen.id,
                 "contracts": sorted(codes), "versions": versions}
        mine = [c for c in sorted(codes) if c in contracts]
        if mine:
            for code in mine:
                contracts[code].append(entry)
        elif codes:
            groups["unlinked"].append(entry)
        else:
            groups[_place(top, live_counts.get(top.id, {}))].append(entry)
    year = getattr(case, "year", None)
    return {"plan": getattr(case, "id", None), "excluded": sorted(basis["вне расчёта"]),
            "contracts": contracts,
            # Переопределения и перечень переопределяемых полей — рядом:
            # экран договора показывает базовое значение и правку вместе, и
            # состав полей должен приходить оттуда же, где он исполняется.
            "overrides": basis["договоры"], "overridable": list(OVERRIDE),
            # Что было в договоре, когда условие правили: изменился договор —
            # экран покажет «в реестре изменилось» (30.09.2026).
            "overrides_base": basis["условия реестра"],
            # Правки строк штатного расписания этого плана (30.09.2026).
            "staff_overrides": {k: _staff_json(v) for k, v in basis["сотрудники"].items()},
            "staff_excluded": sorted(basis["сотрудники вне расчёта"]),
            # Правки и флажки плана по строкам, которых в реестре уже нет.
            "staff_orphans": sorted((set(basis["сотрудники"]) | basis["сотрудники вне расчёта"])
                                    - {str(c) for (c,) in db.query(Employee.code).distinct()}),
            "packages": {code: package(db, code, year) for code in known}, **groups}


def _plural(n, one, few, many):
    n = abs(int(n)) % 100
    if 10 < n < 20:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def _number(v):
    return ("%.2f" % v).rstrip("0").rstrip(".").replace(".", ",")


def package(db, code, year=None):
    """Состав пакета договора для строки над каталогом (UI-006).

    Срок и ФОТ — статус и источник: сами числа стоят в таблице договоров.
    У трудоёмкости, нормативов и кассы ещё и значение — в таблице их нет.
    Касса без графика — не пробел, а допущение: фонд раскладывается ровно.
    """
    c = db.query(Contract).filter_by(code=code).order_by(Contract.id).first()
    vals = (db.query(ContractValue).filter_by(contract_code=code)
            .order_by(ContractValue.id).all())
    roles = []
    # Значения — не только состояние: экономист перед запуском смотрит на
    # числа, а имя файла на месте значения читается как значение. Откуда
    # взято — отдельным полем, оно уходит в подсказку.
    start = next((v for v in vals if v.field == "срок: начало" and v.text), None)
    end = next((v for v in vals if v.field == "срок: окончание" and v.text), None)
    if c is not None and c.date_from and c.date_to:
        roles.append({"key": "term", "title": "Срок", "state": "ok", "source": c.source,
                      "origin": "реестр", "value": "%s – %s" % (c.date_from, c.date_to)})
    elif start is not None and end is not None:
        roles.append({"key": "term", "title": "Срок", "state": "ok", "source": start.source,
                      "origin": "пакет", "value": "%s – %s" % (start.text, end.text)})
    else:
        roles.append({"key": "term", "title": "Срок", "state": "missing"})
    title = "ФОТ %s" % year if year else "ФОТ"
    ozp = [v for v in vals if v.field == "основная заработная плата" and v.value
           and (year is None or v.year == year)]
    if c is not None and c.fund is not None:
        roles.append({"key": "fund", "title": title, "state": "ok", "source": c.source,
                      "origin": "реестр", "value": "%s ₽" % _money(c.fund)})
    elif ozp:
        # Как в сборке входа: ФОТ года — основная зарплата периодов года из
        # последнего загруженного документа. Без года (библиотека) — по годам.
        by_year = {}
        for v in ozp:
            by_year.setdefault(v.year, {}).setdefault(v.document_id or 0, []).append(v)
        parts = []
        for y in sorted(by_year, key=lambda k: k or 0):
            last = by_year[y][max(by_year[y])]
            total = _money(round(sum(v.value for v in last), 2))
            parts.append(("%s ₽" % total) if year else ("%s — %s ₽" % (y or "год?", total)))
        roles.append({"key": "fund", "title": title, "state": "ok", "source": ozp[-1].source,
                      "origin": "пакет", "value": "; ".join(parts)})
    else:
        roles.append({"key": "fund", "title": title, "state": "missing"})
    labor = [r for r in db.query(LaborRow).filter_by(contract_code=code).all()
             if year is None or r.year in (None, year)]
    # Та же трудоёмкость, что уйдёт в расчёт: один документ на год.
    labor, _dropped = _latest_labor(labor)
    if labor:
        # Одно число, а не фраза: «3 должности · 32 чел.-мес.» в графе списка
        # читалось как пересказ («странное описание», пользователь 20.09.2026),
        # да и «·» части не связывает (UI-015). Должности — на вкладке
        # трудоёмкости договора, где их и сравнивают.
        roles.append({"key": "labor", "title": "Трудоёмкость", "state": "ok",
                      "value": "%s чел.-мес." % _number(
                          sum(r.person_months or 0 for r in labor)),
                      "source": ", ".join(sorted({r.source for r in labor if r.source}))})
    else:
        roles.append({"key": "labor", "title": "Трудоёмкость", "state": "missing"})
    # Норматив, от которого считается стоимость чел.-мес., — среднемесячная
    # зарплата года. Взносы 30 % стоят и в пустом образце, пакетом их нет.
    salary = [v for v in vals if v.field.startswith("среднемесячная") and v.value and v.year]
    if salary:
        years = sorted({v.year for v in salary})
        roles.append({"key": "norms", "title": "Нормативы", "state": "ok",
                      "value": ("%s–%s" % (years[0], years[-1])) if len(years) > 1
                      else str(years[0]),
                      "source": ", ".join(sorted({v.source for v in salary if v.source}))})
    else:
        roles.append({"key": "norms", "title": "Нормативы", "state": "missing"})
    # Роли «Касса» нет: поступления в расчёт не подаются, весь ФОТ договора
    # считается поступившим в первый месяц выплат (ПРАВИЛА_РЕШАТЕЛЯ,
    # правило 21; SOURCE-009). Прежнее «ровно по месяцам — допущение» теперь
    # не допущение, а правило, и в пакете ему делать нечего.
    return roles


def describe(db, case):
    """Выбор плана словами — для контекста чата."""
    basis = read(case)
    lines = []
    if basis["вне расчёта"]:
        lines.append("Договоры вне расчёта этого плана: %s."
                     % ", ".join(sorted(basis["вне расчёта"])))
    if basis["сотрудники вне расчёта"]:
        lines.append("Строки штатного расписания вне расчёта этого плана: %s."
                     % ", ".join(sorted(basis["сотрудники вне расчёта"])))
    import facts as facts_mod
    through = facts_mod.through(db, getattr(case, "year", None))
    if through:
        lines.append("Факт начислений по %s включительно: эти месяцы расчёт не "
                     "пересчитывает." % facts_mod.MONTH_WORDS[through - 1])
    if basis["версии"]:
        all_chains = chains(db)
        for root in sorted(basis["версии"]):
            items = all_chains.get(root)
            if not items:
                continue
            top, chosen = head(items), used_version(basis, root, items)
            if chosen.id != top.id:
                lines.append("По документу «%s» план считает версию %s, последняя — %s."
                             % (top.name, chosen.version or 1, top.version or 1))
    return lines
