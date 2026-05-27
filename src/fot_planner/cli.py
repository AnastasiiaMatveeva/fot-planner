"""CLI для планирования ФОТ без веб-интерфейса."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fot_planner.excel_io import create_template
from fot_planner.planner import run_planning


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Планирование ФОТ по договорам НИОКР (Excel in/out, без UI)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init-template", help="Создать шаблон входного Excel")
    init_p.add_argument("-o", "--output", default="data/input_template.xlsx")

    solve_p = sub.add_parser("solve", help="Рассчитать план и сохранить в Excel")
    solve_p.add_argument("-i", "--input", required=True, help="Входной Excel с сотрудниками и договорами")
    solve_p.add_argument("-o", "--output", required=True, help="Файл результата")
    solve_p.add_argument(
        "--plan",
        help="Excel с листом plan (lock=yes) для пересчёта с фиксациями; по умолчанию = --output",
    )
    solve_p.add_argument("--time-limit", type=int, default=120, help="Лимит решателя, сек")

    args = parser.parse_args(argv)

    if args.command == "init-template":
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        create_template(out)
        print(f"Шаблон создан: {out.resolve()}")
        return 0

    if args.command == "solve":
        result = run_planning(
            input_path=args.input,
            output_path=args.output,
            plan_override_path=args.plan,
            time_limit_sec=args.time_limit,
        )
        print(f"Статус: {result.solver_status}")
        print(f"Назначений: {len(result.allocations)}, дефицитов: {len(result.deficits)}")
        print(f"Целевая функция: {result.objective_value:.2f}, время: {result.solve_time_sec} с")
        print(f"Результат: {Path(args.output).resolve()}")
        if result.conflicts:
            print("Конфликты/ошибки:")
            for c in result.conflicts[:10]:
                print(f"  [{c.code}] {c.message}")
            return 2 if result.solver_status == "VALIDATION_FAILED" else 1
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
