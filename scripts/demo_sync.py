# -*- coding: utf-8 -*-
"""Демонстрация: план и реестр — «в реестре изменилось» и снятие правки.

Пользователь 30.09.2026: «подумай, как синхронизироваться с реестром, а как
кастомить»; на предложение «реестр изменил то, что план переписал, —
подсвечивать; реестр сравнялся с правкой — снимать её автоматически»: «ну
сделай»; «и тест сделаешь? — я имею в виду тесты с результатами,
демонстрационное». Скрипт проходит сценарий через API, как экономист руками,
и печатает, что вышло на каждом шаге:

  зарплата месяца: план 90 000 при реестре 100 000 → реестр 95 000 → 90 000;
  условие договора «окончание»: план 30.11 при договоре 31.12 → 31.10 → 30.11.

На каждом шаге видно, что правка помнит реестр, что изменившийся реестр
правку не трогает, но отмечен, и что сравнявшийся реестр снимает правку сам.
В конце реестр и план возвращаются как были.

    .venv/Scripts/python.exe scripts/demo_sync.py --base http://127.0.0.1:8771 --plan 14

Запускать на песочнице (конфигурация «app-sandbox» в .claude/launch.json —
копия базы во временной папке): сценарий правит реестр.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь")


def money(v):
    return "—" if v is None else "{:,.0f}".format(float(v)).replace(",", " ")


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def __call__(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if body is None else
                                     json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise SystemExit("%s %s → %s %s" % (method, path, e.code,
                                                 e.read().decode("utf-8")[:300]))


class Protocol:
    def __init__(self):
        self.ok = True

    def step(self, text):
        print("\n" + text)

    def see(self, text):
        print("   " + text)

    def check(self, cond, text):
        print("   %s %s" % ("✓" if cond else "✗", text))
        self.ok = self.ok and bool(cond)


def salary_demo(api, p, plan, year, code, month):
    rows = api("GET", "/api/case/%d/data" % plan)["salary"]["rows"]
    row = next((r for r in rows if r["code"] == code), None)
    before = row["months"][month - 1] if row else None
    name = MONTHS[month - 1]
    p.step("1. Зарплата %s за %s: в реестре 100 000, в плане правка 90 000" % (code, name))
    api("PUT", "/api/salary_months", {"code": code, "year": year, "month": month, "amount": 100000})
    b = api("POST", "/api/case/%d/basis" % plan,
            {"employee": code, "fields": {"зарплата": {str(month): "90000"}}})
    o = b["staff_overrides"].get(code, {})
    p.see("план: %s, реестр при правке: %s" % (money(o.get("зарплата", {}).get(str(month))),
                                              money(o.get("реестр", {}).get("м%d" % month))))
    p.check(o.get("зарплата", {}).get(str(month)) == 90000
            and o.get("реестр", {}).get("м%d" % month) == 100000,
            "правка плана помнит, поверх какого значения реестра она сделана")

    p.step("2. Реестр изменился: 95 000")
    api("PUT", "/api/salary_months", {"code": code, "year": year, "month": month, "amount": 95000})
    b = api("GET", "/api/case/%d/basis" % plan)
    o = b["staff_overrides"].get(code, {})
    p.see("план: %s, реестр сейчас: 95 000, при правке было: %s"
          % (money(o.get("зарплата", {}).get(str(month))), money(o.get("реестр", {}).get("м%d" % month))))
    p.check(o.get("зарплата", {}).get(str(month)) == 90000,
            "правка осталась — решает экономист, а не реестр")
    p.check(o.get("реестр", {}).get("м%d" % month) == 100000,
            "на экране ячейка подсвечена: «при правке в реестре было 100 000 — реестр изменился»")

    p.step("3. Реестр сравнялся с планом: 90 000")
    api("PUT", "/api/salary_months", {"code": code, "year": year, "month": month, "amount": 90000})
    b = api("GET", "/api/case/%d/basis" % plan)
    o = b["staff_overrides"].get(code, {})
    p.see("снято: %s" % ("; ".join(b.get("dissolved") or []) or "ничего"))
    p.check("зарплата" not in o, "правки больше нет — дальше план идёт за реестром")
    p.check(any(("зарплата, %s" % name) in x for x in b.get("dissolved") or []),
            "экран сообщает: «Реестр сравнялся с правкой плана, правка снята»")

    api("PUT", "/api/salary_months", {"code": code, "year": year, "month": month,
                                      "amount": "" if before is None else before})


def contract_demo(api, p, plan, code):
    ctr = next(c for c in api("GET", "/api/case/%d/data" % plan)["contracts"] if c["code"] == code)
    orig = ctr.get("to") or ""
    p.step("4. Договор %s: в реестре окончание %s, в плане правка 30.11" % (code, orig or "не задано"))
    b = api("POST", "/api/case/%d/basis" % plan, {"contract": code,
                                                  "fields": {"окончание": "30.11"
                                                             + "." + (orig[-4:] or "2026")}})
    end = "30.11." + (orig[-4:] or "2026")
    p.see("план: %s, договор при правке: %s" % (b["overrides"].get(code, {}).get("окончание"),
                                               b["overrides_base"].get(code, {}).get("окончание")))
    p.check(b["overrides"].get(code, {}).get("окончание") == end
            and b["overrides_base"].get(code, {}).get("окончание") == orig,
            "правка условия помнит значение договора")

    p.step("5. Договор в реестре изменился: 31.10")
    api("POST", "/api/contracts/%s" % code, {"fields": {"date_to": "31.10." + end[-4:]}})
    b = api("GET", "/api/case/%d/basis" % plan)
    p.see("план: %s, в договоре сейчас: 31.10, при правке было: %s"
          % (b["overrides"].get(code, {}).get("окончание"),
             b["overrides_base"].get(code, {}).get("окончание")))
    p.check(b["overrides"].get(code, {}).get("окончание") == end, "правка осталась")
    p.check(b["overrides_base"].get(code, {}).get("окончание") == orig,
            "на экране графа «В договоре» подсвечена: «при правке в договоре было %s»" % orig)

    p.step("6. Договор сравнялся с планом: %s" % end)
    api("POST", "/api/contracts/%s" % code, {"fields": {"date_to": end}})
    b = api("GET", "/api/case/%d/basis" % plan)
    p.see("снято: %s" % ("; ".join(b.get("dissolved") or []) or "ничего"))
    p.check("окончание" not in b["overrides"].get(code, {}), "правки условия больше нет")
    p.check(any(code + ": окончание" in x for x in b.get("dissolved") or []),
            "экран сообщает, что правка снята")

    api("POST", "/api/contracts/%s" % code, {"fields": {"date_to": orig}})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8771")
    ap.add_argument("--plan", type=int, required=True)
    ap.add_argument("--employee", default=None, help="код строки; по умолчанию первая")
    ap.add_argument("--contract", default=None, help="шифр; по умолчанию первый в плане")
    ap.add_argument("--month", type=int, default=3)
    args = ap.parse_args()
    api, p = Api(args.base), Protocol()
    plans = api("GET", "/api/cases")
    plans = plans.get("cases", plans) if isinstance(plans, dict) else plans
    plan = next(c for c in plans if c["id"] == args.plan)
    data = api("GET", "/api/case/%d/data" % args.plan)
    code = args.employee or data["employees"][0]["code"]
    excluded = set(api("GET", "/api/case/%d/basis" % args.plan).get("excluded") or [])
    contract = args.contract or next(c["code"] for c in data["contracts"]
                                     if c["code"] not in excluded)
    print("Демонстрация: план «%s», сотрудник %s, договор %s" % (plan["title"], code, contract))
    salary_demo(api, p, args.plan, plan["year"], code, args.month)
    contract_demo(api, p, args.plan, contract)
    print("\nИТОГ: %s" % ("всё сошлось" if p.ok else "есть расхождения"))
    return 0 if p.ok else 1


if __name__ == "__main__":
    sys.exit(main())
