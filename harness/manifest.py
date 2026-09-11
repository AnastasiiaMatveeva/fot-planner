# -*- coding: utf-8 -*-
"""Стенд снимка прогона: артефакты по хешу, канонический JSON, версия кода,
снимок до и после расчёта, сверка воспроизводимости.

Всё на временных папках; решатель не запускается — снимок строится и
проверяется на подставных байтах входа и результата.

    .venv/Scripts/python.exe harness/manifest.py
    .venv/Scripts/python.exe harness/manifest.py --only с04
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from fot_planner.harness_local import manifest as mf  # noqa: E402
from fot_planner.harness_local.artifacts import ArtifactStore  # noqa: E402

CASES = []


def case(num, title):
    def wrap(fn):
        CASES.append((num, title, fn))
        return fn
    return wrap


def expect(cond, msg, out):
    if not cond:
        out.append(msg)


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)
    return path


@case("с01", "артефакт кладётся по хешу один раз; подмена байтов на диске видна")
def _c01(tmp):
    out, store = [], ArtifactStore(os.path.join(tmp, "art"))
    p = write(os.path.join(tmp, "in.bin"), b"input-bytes-1")
    d1 = store.put_file(p)
    t1 = os.path.getmtime(store.path(d1))
    time.sleep(0.05)
    d2 = store.put_file(write(os.path.join(tmp, "in2.bin"), b"input-bytes-1"))
    expect(d1 == d2, "одинаковые байты дали разные хеши", out)
    expect(os.path.getmtime(store.path(d1)) == t1, "повторная запись перезаписала артефакт", out)
    expect(store.verify(d1), "свежий артефакт не проходит сверку", out)
    with open(store.path(d1), "ab") as f:
        f.write(b"x")
    expect(not store.verify(d1), "подменённый артефакт прошёл сверку", out)
    try:
        store.path("не-хеш")
        out.append("принят не-хеш")
    except ValueError:
        pass
    return out


@case("с02", "канонический JSON: порядок ключей не влияет, NaN отклоняется, кириллица цела")
def _c02(tmp):
    out = []
    a = mf.canonical_json({"б": 1, "а": {"y": [1, 2], "x": "ё"}})
    b = mf.canonical_json({"а": {"x": "ё", "y": [1, 2]}, "б": 1})
    expect(a == b, "порядок ключей изменил байты", out)
    expect("ё".encode("utf-8") in a and b" " not in a, "JSON не компактный или не UTF-8: %r" % a[:40], out)
    try:
        mf.canonical_json({"x": float("nan")})
        out.append("NaN принят")
    except ValueError:
        pass
    return out


@case("с03", "версия кода: устойчива при повторе, меняется от правки любого файла пакета")
def _c03(tmp):
    out = []
    src = os.path.join(tmp, "pkg")
    os.makedirs(os.path.join(src, "sub"))
    write(os.path.join(src, "a.py"), b"x = 1\n")
    write(os.path.join(src, "sub", "b.py"), b"y = 2\n")
    v1, v2 = mf.code_version(src), mf.code_version(src)
    expect(v1["code_sha256"] == v2["code_sha256"] and v1["files"] == 2, "версия нестабильна: %s %s" % (v1, v2), out)
    write(os.path.join(src, "sub", "b.py"), b"y = 3\n")
    expect(mf.code_version(src)["code_sha256"] != v1["code_sha256"], "правка файла не изменила версию", out)
    real = mf.code_version(mf.default_src_root())
    expect(real["files"] > 10 and real["dependencies"].get("pyomo"), "версия настоящего пакета пуста: %s" % real, out)
    return out


@case("с04", "черновик закрепляет вход, итог — результат; снимок хранится по хешу и читается обратно")
def _c04(tmp):
    out, store = [], ArtifactStore(os.path.join(tmp, "art"))
    src = write(os.path.join(tmp, "in.xlsx"), b"INPUT")
    ref = write(os.path.join(tmp, "ref.xlsx"), b"REF")
    draft = mf.build_manifest(run_id=7, case_id=2, executor="h:1:ab", input_sha256=store.put_file(src),
                              reference_sha256=store.put_file(ref), settings={"допуск": 0.05},
                              sources={"документы": []}, code=mf.code_version(mf.default_src_root()),
                              command=["fot-planner.exe", "solve"], time_limit_sec="240")
    d1 = mf.store_manifest(store, draft)
    expect(draft["output"] is None and draft["audit"] is None, "черновик уже содержит итог", out)
    expect(mf.verify_manifest(store, draft) == [], "черновик не сверяется: %s" % mf.verify_manifest(store, draft), out)
    res = write(os.path.join(tmp, "out.xlsx"), b"RESULT")
    done = mf.finalize_manifest(draft, output_sha256=store.put_file(res), solver_status="OPTIMAL",
                                audit_status="OK", audit_lines=[], seconds=12.5)
    d2 = mf.store_manifest(store, done)
    expect(d1 != d2, "итог не изменил хеш снимка", out)
    back = mf.load_manifest(store, d2)
    expect(back["output"]["sha256"] == store.put_file(res) and back["solver"]["status"] == "OPTIMAL"
           and back["audit"]["status"] == "OK" and back["input"]["sha256"] == draft["input"]["sha256"],
           "снимок прочитан не тем, чем записан", out)
    expect(mf.manifest_digest(back) == d2, "хеш прочитанного снимка не совпал с именем артефакта", out)
    return out


@case("с05", "сверка: изменённый байт входа или результата обнаруживается; путь к живому файлу снимком не является")
def _c05(tmp):
    out, store = [], ArtifactStore(os.path.join(tmp, "art"))
    src = write(os.path.join(tmp, "in.xlsx"), b"INPUT")
    res = write(os.path.join(tmp, "out.xlsx"), b"RESULT")
    m = mf.finalize_manifest(
        mf.build_manifest(run_id=1, case_id=1, executor="h", input_sha256=store.put_file(src),
                          reference_sha256=None, settings={}, sources={}, code={"code_sha256": "0" * 64},
                          command=["x"], time_limit_sec=1),
        output_sha256=store.put_file(res), solver_status="OPTIMAL", audit_status="OK", audit_lines=[], seconds=1)
    expect(mf.verify_manifest(store, m) == [], "исходный снимок не сверяется", out)
    write(src, b"INPUT-changed")             # живой файл перезаписан — снимку всё равно
    expect(mf.verify_manifest(store, m) == [], "перезапись живого файла сломала снимок, хотя артефакт цел", out)
    with open(store.path(m["output"]["sha256"]), "wb") as f:
        f.write(b"RESULT-tampered")            # подмена артефакта результата
    problems = mf.verify_manifest(store, m)
    expect(any("output" in p and "не совпадают" in p for p in problems), "подмена результата не найдена: %s" % problems, out)
    os.remove(store.path(m["input"]["sha256"]))
    problems = mf.verify_manifest(store, m)
    expect(any("input" in p and "нет в хранилище" in p for p in problems), "пропажа входа не найдена: %s" % problems, out)
    return out


@case("с06", "сервис: прогон получает снимок, маршрут отдаёт его вместе со сверкой")
def _c06(tmp):
    out = []
    os.environ["FOT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["FOT_SKIP_ORPHANS"] = "1"
    sys.path.insert(0, os.path.join(ROOT, "app"))
    for mod in ("main", "chat", "db", "agents", "intake", "reference", "rules"):
        sys.modules.pop(mod, None)
    import db as d
    import main as app_main
    s = d.session()
    c = d.Case(title="Стенд снимка", year=2026, stage="сбор данных")
    s.add(c)
    s.commit()
    run, _ = app_main._start_run(s, c.id, {"допуск": 0.05}, operation_id="snap-1")
    src = write(os.path.join(tmp, "in.xlsx"), b"INPUT")
    res = write(os.path.join(tmp, "out.xlsx"), b"RESULT")
    draft = app_main._manifest_draft(run, src, {"допуск": 0.05}, {"документы": []},
                                     ["C:/x/fot-planner.exe", "solve", "-i", src], "240")
    s.commit()
    expect(run.manifest_sha256 and len(run.manifest_sha256) == 64, "черновик не записан в прогон", out)
    expect(draft["solver"]["command"][0] == "fot-planner.exe", "в снимке абсолютный путь к exe: %s" % draft["solver"]["command"], out)
    app_main._manifest_final(run, draft, res, "OPTIMAL", "OK", [], 3.0)
    s.commit()
    got = app_main.run_manifest(c.id, run.id)
    expect(got["sha256"] == run.manifest_sha256 and got["problems"] == [], "маршрут вернул не то: %s" % got.get("problems"), out)
    expect(got["manifest"]["output"]["sha256"] and got["manifest"]["solver"]["code_sha256"], "в снимке нет результата или версии кода", out)
    art = d.ARTIFACT_DIR
    expect(os.path.isdir(art) and len([p for _, _, f in os.walk(art) for p in f]) >= 4,
           "в хранилище меньше четырёх артефактов (вход, справочник, два снимка)", out)
    s.close()
    d.engine.dispose()
    return out


@case("с07", "неудачный прогон тоже получает итоговый снимок: статус есть, результата нет")
def _c07(tmp):
    out = []
    os.environ["FOT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["FOT_SKIP_ORPHANS"] = "1"
    sys.path.insert(0, os.path.join(ROOT, "app"))
    for mod in ("main", "chat", "db", "agents", "intake", "reference", "rules"):
        sys.modules.pop(mod, None)
    import db as d
    import main as app_main
    s = d.session()
    c = d.Case(title="Стенд снимка", year=2026, stage="сбор данных")
    s.add(c)
    s.commit()
    run, _ = app_main._start_run(s, c.id, {}, operation_id="snap-fail")
    src = write(os.path.join(tmp, "in.xlsx"), b"INPUT")
    draft = app_main._manifest_draft(run, src, {}, {"документы": []}, ["fot-planner.exe", "solve"], "240")
    digest = run.manifest_sha256
    run.status = "нет решения"
    s.commit()
    done = app_main._manifest_close(s, run.id, draft, digest, os.path.join(tmp, "missing.xlsx"), "", [], 3.2)
    expect(done is not None and done["solver"]["status"] == "нет решения" and done["output"] is None,
           "итог отказа не записан: %s" % (done and done["solver"]), out)
    expect(run.manifest_sha256 != digest, "ссылка в прогоне осталась на черновике", out)
    again = app_main._manifest_close(s, run.id, draft, digest, None, "", [], 3.2)
    expect(again is None, "повторное закрытие перезаписало итог", out)
    got = app_main.run_manifest(c.id, run.id)
    expect(got["problems"] == [] and got["manifest"]["audit"]["status"] == "NOT_RUN", "маршрут: %s" % got.get("problems"), out)
    s.close()
    d.engine.dispose()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    rows, failed = [], 0
    for num, title, fn in CASES:
        if only and num not in only:
            continue
        tmp = tempfile.mkdtemp(prefix="manifest_case_")
        t0 = time.time()
        try:
            bad = fn(tmp) or []
        except Exception as exc:  # noqa: BLE001
            bad = ["ошибка стенда: %s: %s" % (type(exc).__name__, str(exc)[:200])]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            os.environ.pop("FOT_DATA_DIR", None)
        rows.append((num, title, bad))
        failed += 1 if bad else 0
        print("[%s] %-9s %4.1f с  %s" % (num, "ПРОВАЛ" if bad else "ОК", time.time() - t0, title))
        for b in bad[:6]:
            print("      ✗", b)
        sys.stdout.flush()
    print("\nитого: %d случаев, провалов %d" % (len(rows), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
