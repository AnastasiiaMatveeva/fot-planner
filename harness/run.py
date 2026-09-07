# -*- coding: utf-8 -*-
"""Стенд разбора документов: прогнать агента по известным случаям и сверить.

Зачем. Качество разбора держится на модели, а модель меняется: другая
подсказка, другая схема, другой бюджет ответа — и зарплата 160 000 из
соседней ячейки молча пропадает. Заметить это глазами по одному прогону
нельзя. Стенд прогоняет разбор по документам с уже проверенным ответом и
говорит, что разошлось.

Как пользоваться::

    .venv/Scripts/python.exe harness/run.py            # все случаи
    .venv/Scripts/python.exe harness/run.py --only uc12 # один, по подстроке

Код возврата ненулевой, если хоть одна проверка не прошла — так стенд можно
поставить перед выкладкой. Модель отвечает не детерминированно, поэтому
счетчики сверяются с допуском, а строки — по ключевым полям.

Что нужно: сервер модели, тот же, что у приложения (берется из .env), и
документы, загруженные в реестр — стенд берет их из базы по имени.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, HERE)

import main  # noqa: E402,F401 — подтягивает .env так же, как сервер
import llm  # noqa: E402
from db import Document, Verdict, session  # noqa: E402

from cases import CASES  # noqa: E402

OK, FAIL, SKIP = "ок", "НЕ ПРОШЛО", "пропущено"

#: Сущность предложения → ключ ответа разбора и поля, по которым строка узнается.
ENTITY_KEYS = {
    "сотрудник": ("employees", ("code",)),
    "договор": ("contracts", ("code",)),
    "трудоемкость": ("labor", ("contract", "position")),
    "поступление": ("inflows", ("contract", "month")),
    "надбавка 120": ("secret", ("employee",)),
    "правило замещения": ("substitutions", ("position",)),
    "должность": ("positions", ("position",)),
}


def _same(expected, got):
    """Совпадает ли значение поля: числа с допуском на копейки, строки без регистра."""
    if isinstance(expected, (int, float)):
        try:
            return abs(float(got) - float(expected)) < 0.5
        except (TypeError, ValueError):
            return False
    return str(got or "").strip().lower() == str(expected).strip().lower()


def _has_row(rows, want):
    """Есть ли среди строк такая, у которой совпали все требуемые поля."""
    return any(all(_same(v, row.get(k)) for k, v in want.items()) for row in rows)


def _check_counts(case, res, out):
    for key, expected in (case.get("counts") or {}).items():
        got = len(res.get(key) or [])
        tol = (case.get("tol") or {}).get(key, 0)
        allowed = expected * tol if isinstance(tol, float) and tol < 1 else tol
        ok = abs(got - expected) <= allowed
        out.append((OK if ok else FAIL, "%s: %d, ожидалось %d%s"
                    % (key, got, expected, " ±%g" % allowed if allowed else "")))


def _check_must(case, res, out):
    for key, wants in (case.get("must") or {}).items():
        rows = res.get(key) or []
        for want in wants:
            found = _has_row(rows, want)
            label = ", ".join("%s=%s" % kv for kv in want.items())
            out.append((OK if found else FAIL, "%s: строка {%s}" % (key, label)))


def _check_none(case, res, out):
    for key in case.get("none") or []:
        got = len(res.get(key) or [])
        out.append((OK if got == 0 else FAIL,
                    "%s: должно быть пусто, найдено %d" % (key, got)))


class _Stub:
    """Документ с диска, не из реестра: только имя и путь."""

    id = None

    def __init__(self, name, path):
        self.name, self.path = name, path


def _file_stub(name):
    for sub in ("harness", "demo", os.path.join("demo", "forms"), "use_cases"):
        path = os.path.join(ROOT, "data", sub, name)
        if os.path.exists(path):
            return _Stub(name, path)
    return None


def run_case(case, doc, cache):
    """Прогнать один случай. Возвращает список (вердикт, пояснение)."""
    out = []
    cls = llm.classify(doc.path, doc.name)

    if case.get("unreadable"):
        ok = not cls.get("ok") and "unavailable" not in cls
        out.append((OK if ok else FAIL, "файл не читается: %s"
                    % (cls.get("error") or "прочитался, а не должен был")))
        return out

    if not cls.get("ok"):
        out.append((FAIL, "вид не определен: %s" % cls.get("error")))
        return out

    if "kind" in case:
        ok = cls["kind"] in case["kind"]
        out.append((OK if ok else FAIL, "вид: «%s», допустимо %s"
                    % (cls["kind"], " / ".join(case["kind"]))))

    if "garbled" in case:
        ok = bool(cls.get("garbled")) == case["garbled"]
        out.append((OK if ok else FAIL, "текст нечитаемый: %s, ожидалось %s"
                    % (bool(cls.get("garbled")), case["garbled"])))
        if case["garbled"]:
            return out          # дальше разбор и не должен идти

    if not any(k in case for k in ("counts", "must", "none", "max_cut")):
        return out

    if case.get("extract"):
        # Якорный разбор форм без модели: детерминированный, поэтому и
        # допуски здесь нулевые. Проверяет то, что реально идет в реестр.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "extract", os.path.join(ROOT, "docs", "ui", "extract.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        p = mod.extract(doc.path)["passport"]
        res = {"ok": True, "employees": p["employees"], "contracts": p["contracts"],
               "inflows": [{"contract": c, "month": m + 1, "amount": v}
                           for c, row in p["inflow"].items()
                           for m, v in enumerate(row) if v],
               "labor": p["labor"]}
    else:
        if doc.name not in cache:
            cache[doc.name] = llm.freeform(doc.path, doc.name)
        res = cache[doc.name]
    if not res.get("ok"):
        out.append((FAIL, "разбор не удался: %s" % res.get("error")))
        return out

    _check_counts(case, res, out)
    _check_must(case, res, out)
    _check_none(case, res, out)
    if "max_cut" in case:
        cut = res.get("обрезано частей") or 0
        out.append((OK if cut <= case["max_cut"] else FAIL,
                    "обрезано частей: %d, допустимо %d" % (cut, case["max_cut"])))
    return out


def check_verdicts(db, by_name, only, cache):
    """Приговоры экономиста как случаи: принятое находится, отклоненное — нет.

    Принятая строка узнается по ключевым полям: значения в ней были верны, и
    если разбор перестал ее находить — это откат. Отклоненная сверяется по
    всем полям: она была неверной именно с такими значениями, и если та же
    ошибка повторилась слово в слово — разбор не стал лучше.
    """
    import json

    rows = db.query(Verdict).order_by(Verdict.document_name, Verdict.id).all()
    if not rows:
        return 0, 0
    total = failed = 0
    by_doc: dict = {}
    for v in rows:
        by_doc.setdefault(v.document_name, []).append(v)
    for name, verdicts in by_doc.items():
        if only and only.lower() not in name.lower():
            continue
        doc = by_name.get(name)
        print("── приговоры по «%s»: %d" % (name[:50], len(verdicts)))
        if doc is None or not os.path.exists(doc.path):
            print("   %s: документа нет в реестре\n" % SKIP)
            continue
        if name not in cache:
            cache[name] = llm.freeform(doc.path, doc.name)
        res = cache[name]
        if not res.get("ok"):
            print("   %s: разбор не удался: %s\n" % (FAIL, res.get("error")))
            failed += 1
            total += 1
            continue
        for v in verdicts:
            key, fields = ENTITY_KEYS.get(v.entity, (None, ()))
            if key is None:
                continue
            want = json.loads(v.fields)
            got = res.get(key) or []
            total += 1
            if v.verdict == "принято":
                probe = {k: want[k] for k in fields if want.get(k) not in (None, "")}
                ok = _has_row(got, probe)
                text = "принятая строка %s %s" % (key, probe)
            else:
                exact = {k: val for k, val in want.items()
                         if val not in (None, "") and k != "место"}
                ok = not _has_row(got, exact)
                text = "отклоненная строка %s не повторилась: %s" % (key, exact)
            if not ok:
                failed += 1
            print("   %-10s %s" % (OK if ok else FAIL, text))
        print()
    _calibration(rows, only)
    return total, failed


def _calibration(rows, only):
    """Сколько строк каждой оценки экономист принял и отклонил.

    Оценка (intake.grade) обещает: «надежно» — можно принимать не глядя.
    Приговоры это обещание проверяют. «Надежно», которое отклоняют, — знак,
    что проверки пропускают ошибку; «сомнительно», которое всегда принимают,
    — что они слишком строги. Это не случай стенда, а показание прибора:
    в счет проверок не идет, но печатается каждый прогон.
    """
    calib = {}
    for v in rows:
        if only and only.lower() not in v.document_name.lower():
            continue
        c = calib.setdefault(v.grade or "без оценки", {"принято": 0, "отклонено": 0})
        c[v.verdict] = c.get(v.verdict, 0) + 1
    if not calib:
        return
    print("── калибровка оценок: принято / отклонено")
    for g in ("надежно", "проверить", "сомнительно", "без оценки"):
        if g not in calib:
            continue
        c = calib[g]
        n = c["принято"] + c["отклонено"]
        note = ""
        if g == "надежно" and n and c["отклонено"] / n > 0.1:
            note = "   внимание: надежных отклонено больше десятой части"
        if g == "сомнительно" and n >= 10 and not c["отклонено"]:
            note = "   внимание: сомнительные всегда принимают — проверки слишком строги"
        print("   %-12s %d / %d%s" % (g, c["принято"], c["отклонено"], note))
    print()


def check_roundtrip(case, doc):
    """Документ по шаблону: реестр → вход решателя → план сходится.

    Реестр берется чистый, в памяти: стенд не должен трогать базу сервиса.
    Документ читается загрузчиком решателя, раскладывается по реестру тем же
    кодом, что в приложении, собирается обратно тем же сборщиком и решается
    тем же исполняемым файлом. Сравнение с решением самого файла — прямое:
    оба обязаны сойтись.
    """
    import subprocess
    import tempfile
    import types

    import build_input
    import intake
    import main as app_main
    import reference
    from db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    exe = app_main.EXE
    if not os.path.exists(exe):
        return SKIP, "решателя нет: %s" % exe
    ctx = intake._solver_context(doc.path)
    if ctx is None:
        return FAIL, "загрузчик решателя не прочитал документ как шаблон"
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    scratch = sessionmaker(bind=engine, autoflush=False, future=True)()
    passport = {}
    try:
        import extract
        passport = extract.extract(doc.path).get("passport") or {}
    except Exception:  # noqa: BLE001 — настройки не обязательны
        pass
    counts = intake._store_context(scratch, ctx, types.SimpleNamespace(id=doc.id, name=doc.name))
    fake_case = types.SimpleNamespace(year=ctx.year, passport=json.dumps(passport, ensure_ascii=False))
    data = app_main._registry_data(scratch, fake_case)
    tmp = tempfile.mkdtemp(prefix="fot_rt_")
    src = os.path.join(tmp, "input.xlsx")
    warn = []
    build_input.build(reference.TEMPLATE, src, data, warn)
    out = os.path.join(tmp, "out.xlsx")
    p = subprocess.run([exe, "solve", "-i", src, "-o", out], capture_output=True,
                       text=True, timeout=600, cwd=ROOT)
    orig = subprocess.run([exe, "solve", "-i", doc.path, "-o", os.path.join(tmp, "orig.xlsx")],
                          capture_output=True, text=True, timeout=600, cwd=ROOT)
    what = ", ".join("%s %d" % (k, v) for k, v in counts.items() if v)
    if orig.returncode != 0:
        return SKIP, "сам файл не решается — сравнивать не с чем (%s)" % what
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or ["—"]
        return FAIL, "из реестра не решается, хотя сам файл решается: %s (%s)" % (tail[0][:160], what)
    return OK, "из реестра решается, как и сам файл (%s)" % what


def main_():
    ap = argparse.ArgumentParser(description="стенд разбора документов")
    ap.add_argument("--only", help="прогнать только случаи, в имени которых есть подстрока")
    ap.add_argument("--no-verdicts", action="store_true",
                    help="не проверять приговоры экономиста, только случаи из cases.py")
    ap.add_argument("--no-solve", action="store_true",
                    help="не гонять решатель на сквозных случаях (solves)")
    args = ap.parse_args()

    name, model, why = llm.provider()
    if name is None:
        print("модель недоступна: %s" % why)
        return 2
    print("модель: %s %s\n" % (name, model))

    db = session()
    by_name = {d.name: d for d in db.query(Document).all()}
    failed = total = 0
    cache: dict = {}          # разбор каждого документа — один раз на прогон

    for case in CASES:
        if args.only and args.only.lower() not in case["doc"].lower():
            continue
        doc = by_name.get(case["doc"])
        title = case["doc"] if len(case["doc"]) <= 60 else case["doc"][:57] + "…"
        print("── %s\n   %s" % (title, case["why"]))
        if doc is None or not os.path.exists(doc.path):
            # Документа нет в реестре — берем файл с диска: демо-набор,
            # образцы форм и сценарии лежат в репозитории. Сквозная проверка
            # через реестр для такого файла невозможна, остальное — можно.
            doc = _file_stub(case["doc"])
        if doc is None:
            print("   %s: документа нет ни в реестре, ни на диске\n" % SKIP)
            continue
        t0 = time.time()
        try:
            checks = run_case(case, doc, cache)
        except Exception as e:  # noqa: BLE001 — стенд должен дойти до конца
            checks = [(FAIL, "ошибка стенда: %s" % e)]
        if case.get("solves") and not args.no_solve:
            doc = by_name.get(case["doc"])
            if doc is not None and os.path.exists(doc.path):
                try:
                    checks.append(check_roundtrip(case, doc))
                except Exception as exc:  # noqa: BLE001 — сбой стенда тоже результат
                    checks.append((FAIL, "сквозная проверка упала: %s" % str(exc)[:200]))
        for verdict, text in checks:
            total += 1
            if verdict == FAIL:
                failed += 1
            print("   %-10s %s" % (verdict, text))
        print("   %.0f с\n" % (time.time() - t0))

    if not args.no_verdicts:
        vt, vf = check_verdicts(db, by_name, args.only, cache)
        total += vt
        failed += vf

    print("итого: проверок %d, не прошло %d" % (total, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main_())
