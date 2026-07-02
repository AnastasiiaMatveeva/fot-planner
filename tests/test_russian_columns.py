from datetime import date

import pandas as pd

from fot_planner.excel import create_template, load_context


def test_template_uses_russian_columns_and_loader_understands_them(tmp_path):
    path = tmp_path / "input.xlsx"
    create_template(path)
    year = date.today().year

    employees = pd.read_excel(path, sheet_name="employees")
    contracts = pd.read_excel(path, sheet_name="contracts")

    assert "фио" in employees.columns
    assert "зарплата" in employees.columns
    assert "тип договора" in contracts.columns
    assert "резерв оклада" not in contracts.columns

    settings = pd.read_excel(path, sheet_name="settings")
    assert "месяцев фот для резерва" not in settings.columns
    assert "разрешить перенос назад" not in settings.columns

    labor = pd.DataFrame(
        [
            {
                "договор": "C001",
                "год": year,
                "трудоемкость": 12,
                "должность": "инженер",
                "средняя стоимость выполнения работ в месяц": 60000,
            }
        ]
    )
    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        labor.to_excel(w, sheet_name="contract_labor", index=False)

    ctx = load_context(path)
    assert ctx.employees[0].full_name == "Иванов Иван Иванович"
    assert ctx.employees[0].monthly_wage == 100_000
    assert ctx.contracts[0].contract_type == "goszakaz"
    assert ctx.labor_plans[0].person_months == 12
    c = ctx.contracts[0]
    assert abs(sum(mb.inflow_amount for mb in c.monthly_budgets) - c.total_fot) < 0.01
    assert c.monthly_budgets


def test_empty_payment_deadlines_default_to_contract_end_date(tmp_path):
    path = tmp_path / "input.xlsx"
    create_template(path)
    year = date.today().year

    contracts = pd.DataFrame(
        [
            {
                "код": "VB01",
                "название": "Внебюджет тест",
                "номер": "1",
                "тип договора": "off_budget",
                "дата начала": f"{year}-01-01",
                "дата окончания": f"{year}-12-31",
                "фот": 1_000_000,
                "оклад разрешен": True,
                "122 разрешена": True,
                "124 разрешена": True,
                "перенос остатков": "",
            }
        ]
    )
    fot_matrix = pd.DataFrame({"договор": ["VB01"]})
    for m in range(1, 13):
        fot_matrix[str(m)] = [100_000]

    with pd.ExcelWriter(path, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        contracts.to_excel(w, sheet_name="contracts", index=False)
        fot_matrix.to_excel(w, sheet_name="fot_matrix", index=False)

    ctx = load_context(path)
    c = next(x for x in ctx.contracts if x.id == "VB01")
    end = date(year, 12, 31)
    assert c.salary_payment_deadline == end
    assert c.allowances_payment_deadline == end
