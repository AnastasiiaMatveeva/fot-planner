"""Quick analysis of demo_long_salary_result.xlsx."""
import pandas as pd

p = "data/demo_long_salary_result.xlsx"
plan = pd.read_excel(p, "план_выплат")
RU = {
    "Январь": 1,
    "Февраль": 2,
    "Март": 3,
    "Апрель": 4,
    "Май": 5,
    "Июнь": 6,
    "Июль": 7,
    "Август": 8,
    "Сентябрь": 9,
    "Октябрь": 10,
    "Ноябрь": 11,
    "Декабрь": 12,
}


def month_num(s):
    return RU.get(str(s).strip(), int(s) if str(s).isdigit() else 0)


print("=== Salary by employee ===")
sal = plan[plan["вид выплаты"] == "оклад"].copy()
sal["m"] = sal["месяц"].map(month_num)
for eid in sorted(sal["табельный номер"].unique()):
    sub = sal[sal["табельный номер"] == eid].sort_values("m")
    print(f"{eid}: {sub['договор'].unique().tolist()}")
print()

print("=== Allowance by month (total per contract) ===")
allw = plan[plan["вид выплаты"] == "надбавка"].copy()
allw["m"] = allw["месяц"].map(month_num)
agg = allw.groupby(["m", "договор"])["сумма"].sum().reset_index()
for m in range(1, 13):
    rows = agg[agg["m"] == m]
    if len(rows):
        parts = ", ".join(f"{r['договор']}={r['сумма']:.0f}" for _, r in rows.iterrows())
        print(f"  m{m:02d}: {parts}")
print()

print("=== FOT spent ===")
print(plan.groupby("договор")["сумма"].sum())
print()
print("Deficits:", len(pd.read_excel(p, "дефициты")))
print("Transfers:", len(pd.read_excel(p, "переносы")))
