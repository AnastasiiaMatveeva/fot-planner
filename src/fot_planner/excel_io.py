"""Импорт и экспорт данных через Excel."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from fot_planner.contract_types import CONTRACT_TYPE_DEFAULTS, KNOWN_CONTRACT_TYPES
from fot_planner.fot_schedule import spread_fot_by_active_months
from fot_planner.labor_rules import planned_labor_groups
from fot_planner.models import (
    AllocationRecord,
    Contract,
    ContractLaborPlan,
    ContractMonthlyBudget,
    ContractPositionRule,
    Employee,
    ManualAssignment,
    ManualProhibition,
    OptimizationWeights,
    PaymentKind,
    PlanningContext,
    PlanningResult,
    PositionReference,
    SalaryStabilityRules,
)
from fot_planner.position_reference import (
    PositionReferenceRow,
    build_position_index,
    default_position_reference,
    default_position_synonyms,
    normalize_position,
    resolve_position,
)

SHEET_EMPLOYEES = "employees"
SHEET_CONTRACTS = "contracts"
SHEET_CONTRACT_POSITIONS = "contract_positions"
SHEET_CONTRACT_LABOR = "contract_labor"
SHEET_CONTRACT_BUDGET = "contract_monthly_budget"
SHEET_FOT_MATRIX = "fot_matrix"
SHEET_FOT_LOCK_MATRIX = "fot_lock_matrix"
SHEET_MIN_BALANCE_MATRIX = "min_balance_matrix"
SHEET_MANUAL_ASSIGNMENTS = "manual_assignments"
SHEET_MANUAL_PROHIBITIONS = "manual_prohibitions"
SHEET_SETTINGS = "settings"
SHEET_PLAN = "plan"
SHEET_DEFICITS = "deficits"
SHEET_CONFLICTS = "conflicts"
SHEET_BALANCES = "contract_balances"
SHEET_META = "meta"
SHEET_POSITION_REFERENCE = "справочник_должностей"
SHEET_POSITION_SYNONYMS = "синонимы_должностей"

COLUMN_ALIASES: dict[str, str] = {
    # Common IDs / dates
    "код": "id",
    "табельный номер": "id",
    "таб. номер": "id",
    "id": "id",
    "фио": "full_name",
    "сотрудник": "full_name",
    "должность": "position",
    "подразделение": "department",
    "кафедра": "department",
    "ставка": "rate",
    "оклад": "salary",
    "надбавка": "allowance",
    "стимулирующая": "incentive",
    "дата начала": "start_date",
    "начало": "start_date",
    "дата окончания": "end_date",
    "окончание": "end_date",
    "разрешенные договоры": "allowed_contracts",
    "запрещенные договоры": "forbidden_contracts",
    # Contracts / contract types
    "название": "name",
    "номер": "number",
    "тип договора": "contract_type",
    "срок освоения": "spend_deadline",
    "фот": "total_fot",
    "фот за год": "total_fot",
    "оклад разрешен": "allow_salary",
    "надбавка разрешена": "allow_allowance",
    "стимулирующая разрешена": "allow_incentive",
    "месяцев после окончания": "months_after_end",
    "перенос остатков": "allow_monthly_carryover",
    "резерв оклада": "require_salary_reserve",
    "полное освоение за дней до срока": "spend_complete_days_before_end",
    # Contract links / limits
    "договор": "contract_id",
    "проект": "contract_id",
    "макс выплата": "max_monthly_payment",
    "макс ставки": "max_positions",
    "максимум ставок": "max_positions",
    "группа взаимозаменяемости": "equivalence_group",
    "уровень": "position_level",
    "оклад по справочнику за 1 ставку": "reference_salary_for_rate",
    "оклад по справочнику": "reference_salary_for_rate",
    "должностной оклад за 1 ставку": "reference_salary_for_rate",
    "как написано": "raw_position",
    "должность из справочника": "canonical_position",
    "трудоемкость": "person_months",
    "трудоёмкость": "person_months",
    "чел-мес": "person_months",
    "месяц": "month",
    # Manual rules / plan
    "месяц с": "month_from",
    "месяц по": "month_to",
    "вид выплаты": "payment_kind",
    "фикс сумма": "fixed_amount",
    "сумма": "amount",
    "фикс": "lock",
    "поступление": "inflow_amount",
    # Settings
    "год": "year",
    "штраф дефицита": "weight_uncovered_salary",
    "штраф компенсации оклада надбавкой": "weight_salary_compensation_via_flex",
    "вес штрафа смены оклада": "weight_salary_switch",
    "штраф смены договора оклада": "weight_salary_switch",
    "макс договоров оклада в год": "max_salary_contracts_per_year",
    "штраф административной сложности выплат": "weight_admin_complexity",
    "штраф дробления переменных выплат": "weight_flex_fragment",
    "штраф дробления надбавок и стимулирующих": "weight_flex_fragment",
    "штраф дробления надбавок": "weight_flex_fragment",
    # Устаревшие имена (читаются в _load_weights как legacy)
    "фиксировать оклад в квартале": "salary_lock_within_quarter",
    "штраф смены оклада в квартале": "penalize_quarterly_salary_switch",
    "штраф договора сотрудника за год": "weight_employee_contract_year_count",
    "штраф договора сотрудника в месяце": "weight_employee_contract_month_count",
    "weight_flex_payment_fragment_count": "weight_flex_fragment",
    "месяцев фот для резерва": "min_fot_months_for_salary_reserve",
    "допуск трудоемкости гоз": "goz_labor_tolerance",
    "вес отклонения равномерного освоения": "weight_uniform_spend_deviation",
    "штраф отклонения от равномерного освоения": "weight_uniform_spend_deviation",
    "вес штрафа смены надбавки": "weight_allowance_switch",
    "вес штрафа смены стимулирующей": "weight_incentive_switch",
    "разрешить перенос назад": "allow_backward_reallocation",
}

RU_MONTHS = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}

_PAYMENT_KIND_RU = {
    "salary": "оклад",
    "allowance": "надбавка",
    "incentive": "стимулирующая",
}


def _canonical_column_name(col) -> str:
    raw = str(col).strip()
    key = raw.lower().replace("ё", "е")
    return COLUMN_ALIASES.get(key, raw)


def _canonicalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={col: _canonical_column_name(col) for col in df.columns})


def _canonicalize_manual_columns(df: pd.DataFrame) -> pd.DataFrame:
    """На листах manual «сотрудник» — табельный номер, не ФИО."""
    out = _canonicalize_columns(df)
    if "employee_id" not in out.columns and "full_name" in out.columns:
        out = out.rename(columns={"full_name": "employee_id"})
    return out


def _parse_date(val) -> date | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return pd.to_datetime(s).date()


def _split_list(val) -> list[str]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return []
    return [x.strip() for x in str(val).split(";") if x.strip()]


def _bool(val, default: bool = False) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "да", "yes", "y")


def _optional_bool(val):
    if val is None or (isinstance(val, float) and pd.isna(val)) or str(val).strip() == "":
        return None
    return _bool(val)


def _optional_float(val) -> float | None:
    if val is None or (isinstance(val, float) and pd.isna(val)) or str(val).strip() == "":
        return None
    return float(val)


def _optional_int(val) -> int | None:
    if val is None or (isinstance(val, float) and pd.isna(val)) or str(val).strip() == "":
        return None
    return int(float(val))


def _load_spend_complete_days(row) -> int | None:
    """
    Полное освоение к сроку: пусто = не требуем, 0 = к сроку, N = за N дней до срока.
    Поддержка старых колонок require_full_spend + spend_days_before_end.
    """
    if "spend_complete_days_before_end" in row.index:
        raw = row.get("spend_complete_days_before_end")
        if raw is not None and not (isinstance(raw, float) and pd.isna(raw)) and str(raw).strip() != "":
            return _optional_int(raw)
    if "require_full_spend" in row.index and _bool(row.get("require_full_spend")):
        return _optional_int(row.get("spend_days_before_end")) or 0
    return None


def load_context(path: str | Path, plan_path: str | Path | None = None) -> PlanningContext:
    path = Path(path)
    xl = pd.ExcelFile(path)

    year = date.today().year
    settings_df: pd.DataFrame | None = None
    if SHEET_SETTINGS in xl.sheet_names:
        settings_df = _canonicalize_columns(pd.read_excel(xl, SHEET_SETTINGS))
        if not settings_df.empty and "year" in settings_df.columns:
            year = int(settings_df.iloc[0]["year"])

    position_reference_rows = _load_position_reference(xl)
    position_synonyms = _load_position_synonyms(xl)
    position_index = build_position_index(position_reference_rows, position_synonyms)
    position_reference = [
        PositionReference(
            position=row.должность,
            equivalence_group=row.группа_взаимозаменяемости,
            level=row.уровень,
            reference_salary_for_rate=row.оклад_по_справочнику_за_1_ставку,
        )
        for row in position_reference_rows
    ]

    employees = _load_employees(
        _canonicalize_columns(pd.read_excel(xl, SHEET_EMPLOYEES)),
        position_index,
    )
    contracts = _load_contracts(_canonicalize_columns(pd.read_excel(xl, SHEET_CONTRACTS)))
    spread_fot_by_active_months(contracts, year)

    if SHEET_CONTRACT_POSITIONS in xl.sheet_names:
        _apply_positions(
            contracts,
            _canonicalize_columns(pd.read_excel(xl, SHEET_CONTRACT_POSITIONS)),
            position_index,
        )
    if SHEET_FOT_MATRIX in xl.sheet_names:
        amounts_df = _canonicalize_columns(pd.read_excel(xl, SHEET_FOT_MATRIX))
        lock_df = None
        if SHEET_FOT_LOCK_MATRIX in xl.sheet_names:
            lock_df = _canonicalize_columns(pd.read_excel(xl, SHEET_FOT_LOCK_MATRIX))
        fill_locks = _read_fot_matrix_fill_locks(path)
        _apply_matrix_budgets(
            contracts,
            amounts_df,
            lock_df,
            year,
            fill_locks=fill_locks,
        )
    elif SHEET_CONTRACT_BUDGET in xl.sheet_names:
        _apply_budgets(
            contracts,
            _canonicalize_columns(pd.read_excel(xl, SHEET_CONTRACT_BUDGET)),
        )
    if SHEET_MIN_BALANCE_MATRIX in xl.sheet_names:
        _apply_matrix_min_balances(
            contracts,
            _canonicalize_columns(pd.read_excel(xl, SHEET_MIN_BALANCE_MATRIX)),
            year,
        )

    labor_plans: list[ContractLaborPlan] = []
    if SHEET_CONTRACT_LABOR in xl.sheet_names:
        labor_plans = _load_contract_labor(
            _canonicalize_columns(pd.read_excel(xl, SHEET_CONTRACT_LABOR)),
            year,
            position_index,
        )

    manual_assignments: list[ManualAssignment] = []
    manual_prohibitions: list[ManualProhibition] = []
    if SHEET_MANUAL_ASSIGNMENTS in xl.sheet_names:
        manual_assignments = _load_manual_assignments(
            _canonicalize_manual_columns(pd.read_excel(xl, SHEET_MANUAL_ASSIGNMENTS))
        )
    if SHEET_MANUAL_PROHIBITIONS in xl.sheet_names:
        manual_prohibitions = _load_manual_prohibitions(
            _canonicalize_manual_columns(pd.read_excel(xl, SHEET_MANUAL_PROHIBITIONS))
        )

    baseline_plan: list[AllocationRecord] | None = None
    plan_source = plan_path or path
    plan_source = Path(plan_source)
    if plan_source.exists() and plan_source.suffix.lower() in (".xlsx", ".xlsm"):
        plan_xl = pd.ExcelFile(plan_source)
        if SHEET_PLAN in plan_xl.sheet_names:
            baseline_plan = _load_plan_overrides(pd.read_excel(plan_xl, SHEET_PLAN), year)

    weights = OptimizationWeights()
    salary_stability = SalaryStabilityRules()
    if settings_df is not None:
        weights = _load_weights(settings_df)
        salary_stability = _load_salary_stability(settings_df)

    return PlanningContext(
        year=year,
        employees=employees,
        contracts=contracts,
        position_reference=position_reference,
        manual_assignments=manual_assignments,
        manual_prohibitions=manual_prohibitions,
        weights=weights,
        salary_stability=salary_stability,
        labor_plans=labor_plans,
        baseline_plan=baseline_plan,
    )


def _load_position_reference(xl: pd.ExcelFile) -> list[PositionReferenceRow]:
    rows = list(default_position_reference())
    if SHEET_POSITION_REFERENCE not in xl.sheet_names:
        return rows

    df = _canonicalize_columns(pd.read_excel(xl, SHEET_POSITION_REFERENCE))
    for _, r in df.iterrows():
        raw_position = str(r.get("position", "")).strip()
        raw_group = str(r.get("equivalence_group", "")).strip()
        if not raw_position or not raw_group:
            continue
        rows.append(
            PositionReferenceRow(
                должность=raw_position,
                группа_взаимозаменяемости=raw_group,
                уровень=_optional_int(r.get("position_level")),
                оклад_по_справочнику_за_1_ставку=_optional_float(
                    r.get("reference_salary_for_rate")
                ),
            )
        )
    return rows


def _load_position_synonyms(xl: pd.ExcelFile) -> dict[str, str]:
    synonyms = default_position_synonyms()
    if SHEET_POSITION_SYNONYMS not in xl.sheet_names:
        return synonyms

    df = _canonicalize_columns(pd.read_excel(xl, SHEET_POSITION_SYNONYMS))
    for _, r in df.iterrows():
        raw = str(r.get("raw_position", "")).strip()
        canonical = str(r.get("canonical_position", "")).strip()
        if raw and canonical:
            synonyms[normalize_position(raw)] = normalize_position(canonical)
    return synonyms


def _load_employees(
    df: pd.DataFrame,
    position_index: dict[str, PositionReferenceRow],
) -> list[Employee]:
    rows: list[Employee] = []
    for _, r in df.iterrows():
        position = str(r["position"]).strip()
        ref = resolve_position(position, position_index)
        rows.append(
            Employee(
                id=str(r["id"]).strip(),
                full_name=str(r.get("full_name", r.get("fio", ""))).strip(),
                position=position,
                department=str(r.get("department", "")).strip(),
                rate=float(r["rate"]),
                salary=float(r["salary"]),
                allowance=float(r.get("allowance", 0) or 0),
                incentive=float(r.get("incentive", 0) or 0),
                start_date=_parse_date(r.get("start_date")),
                end_date=_parse_date(r.get("end_date")),
                allowed_contracts=_split_list(r.get("allowed_contracts")),
                forbidden_contracts=_split_list(r.get("forbidden_contracts")),
                equivalence_group=ref.группа_взаимозаменяемости if ref else None,
                position_level=ref.уровень if ref else None,
                reference_salary_for_rate=ref.оклад_по_справочнику_за_1_ставку if ref else None,
            )
        )
    return rows


def _type_defaults(contract_type: str) -> dict[str, bool | int | None]:
    generic: dict[str, bool | int | None] = {
        "allow_monthly_carryover": True,
        "months_after_end": 0,
        "spend_complete_days_before_end": None,
        "allow_salary": True,
        "allow_allowance": True,
        "allow_incentive": True,
    }
    known = CONTRACT_TYPE_DEFAULTS.get(contract_type)
    if known is None:
        return generic
    return {**generic, **known}


def _parse_months_after_end_and_spend(
    row: pd.Series, contract_type: str
) -> tuple[int, int | None]:
    """
    months_after_end: 0 — только до окончания; 2 — +2 мес.; -1 — освоение за 20 дней до срока.
    """
    defaults = _type_defaults(contract_type)
    spend_days = _load_spend_complete_days(row)
    if spend_days is None:
        spend_days = defaults["spend_complete_days_before_end"]
    months_raw = _optional_int(row.get("months_after_end"))
    if months_raw == -1:
        return 0, spend_days if spend_days is not None else 20
    if months_raw is not None:
        return months_raw, spend_days
    return int(defaults["months_after_end"] or 0), spend_days


def _load_contracts(df: pd.DataFrame) -> list[Contract]:
    rows: list[Contract] = []
    for _, r in df.iterrows():
        ctype = str(r["contract_type"]).strip()
        defaults = _type_defaults(ctype)
        months_after_end, spend_complete_days = _parse_months_after_end_and_spend(r, ctype)
        carryover = _optional_bool(r.get("allow_monthly_carryover"))
        rows.append(
            Contract(
                id=str(r["id"]).strip(),
                name=str(r.get("name", "")).strip(),
                number=str(r.get("number", "")).strip(),
                contract_type=ctype,
                start_date=_parse_date(r["start_date"]),
                end_date=_parse_date(r["end_date"]),
                spend_deadline=_parse_date(r.get("spend_deadline")),
                total_fot=float(r["total_fot"]),
                allow_salary=_bool(r.get("allow_salary"), bool(defaults["allow_salary"])),
                allow_allowance=_bool(
                    r.get("allow_allowance"), bool(defaults["allow_allowance"])
                ),
                allow_incentive=_bool(
                    r.get("allow_incentive"), bool(defaults["allow_incentive"])
                ),
                months_after_end=months_after_end,
                allow_monthly_carryover=(
                    carryover
                    if carryover is not None
                    else bool(defaults["allow_monthly_carryover"])
                ),
                require_salary_reserve=_optional_bool(r.get("require_salary_reserve")),
                spend_complete_days_before_end=spend_complete_days,
            )
        )
    return rows


def _apply_positions(
    contracts: list[Contract],
    df: pd.DataFrame,
    position_index: dict[str, PositionReferenceRow],
) -> None:
    by_id = {c.id: c for c in contracts}
    for _, r in df.iterrows():
        cid = str(r["contract_id"]).strip()
        if cid not in by_id:
            continue

        position = str(r["position"]).strip()
        ref = resolve_position(position, position_index)
        max_pay = r.get("max_monthly_payment")
        if pd.isna(max_pay) and ref is not None:
            max_pay = ref.оклад_по_справочнику_за_1_ставку

        by_id[cid].position_rules.append(
            ContractPositionRule(
                contract_id=cid,
                position=position,
                max_monthly_payment=float(max_pay) if pd.notna(max_pay) else None,
                max_positions=_optional_float(r.get("max_positions")),
                equivalence_group=ref.группа_взаимозаменяемости if ref else None,
                position_level=ref.уровень if ref else None,
                reference_salary_for_rate=ref.оклад_по_справочнику_за_1_ставку if ref else None,
            )
        )


_MONTH_ALIASES: dict[str, int] = {
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "10": 10,
    "11": 11,
    "12": 12,
    "янв": 1,
    "январь": 1,
    "фев": 2,
    "февраль": 2,
    "мар": 3,
    "март": 3,
    "апр": 4,
    "апрель": 4,
    "май": 5,
    "июн": 6,
    "июнь": 6,
    "июл": 7,
    "июль": 7,
    "авг": 8,
    "август": 8,
    "сен": 9,
    "сентябрь": 9,
    "окт": 10,
    "октябрь": 10,
    "ноя": 11,
    "ноябрь": 11,
    "дек": 12,
    "декабрь": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _month_from_column(name) -> int | None:
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return None
    key = str(name).strip().lower().replace(".", "")
    if key in _MONTH_ALIASES:
        return _MONTH_ALIASES[key]
    try:
        m = int(float(key))
        if 1 <= m <= 12:
            return m
    except ValueError:
        pass
    return None


def _matrix_month_columns(df: pd.DataFrame) -> dict[int, str]:
    """Первая колонка — id проекта, остальные — месяцы 1–12."""
    mapping: dict[int, str] = {}
    for col in df.columns[1:]:
        month = _month_from_column(col)
        if month is not None:
            mapping[month] = col
    return mapping


_WHITE_FILL_RGB = frozenset({"FFFFFF", "FFFFFFFF", "00FFFFFF"})


def _parse_matrix_cell_value(val) -> tuple[float | None, bool]:
    """Сумма из ячейки; lock по суффиксу * или ! или (фикс)."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None, False
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val), False

    locked = False
    s = str(val).strip()
    for marker in ("(фикс)", "(lock)", "(l)"):
        if s.lower().endswith(marker):
            locked = True
            s = s[: -len(marker)].strip()
            break
    if s.endswith(("*", "!")):
        locked = True
        s = s[:-1].strip()
    s = s.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not s:
        return None, locked
    try:
        return float(s), locked
    except ValueError:
        return None, locked


def _cell_has_lock_fill(cell) -> bool:
    """Заливка ячейки (жёлтая и т.п.) = фиксация суммы на fot_matrix."""
    fill = cell.fill
    if fill is None or fill.fill_type in (None, "none"):
        return False
    if fill.fill_type != "solid":
        return True
    color = fill.fgColor
    if color is None:
        return False
    if color.type == "rgb" and color.rgb:
        rgb = color.rgb.upper()
        if len(rgb) == 8:
            rgb = rgb[2:]
        return rgb not in _WHITE_FILL_RGB
    if color.type == "indexed" and color.indexed is not None:
        return color.indexed not in (9, 64, 65)
    if color.type == "theme":
        return True
    return False


def _read_fot_matrix_fill_locks(path: Path) -> dict[tuple[str, int], bool]:
    try:
        from openpyxl import load_workbook
    except ImportError:
        return {}

    try:
        wb = load_workbook(path, data_only=False, read_only=False)
    except Exception:
        return {}
    if SHEET_FOT_MATRIX not in wb.sheetnames:
        wb.close()
        return {}

    ws = wb[SHEET_FOT_MATRIX]
    month_by_col: dict[int, int] = {}
    for col_idx in range(2, ws.max_column + 1):
        month = _month_from_column(ws.cell(1, col_idx).value)
        if month is not None:
            month_by_col[col_idx] = month

    locks: dict[tuple[str, int], bool] = {}
    for row_idx in range(2, ws.max_row + 1):
        raw_id = ws.cell(row_idx, 1).value
        if raw_id is None:
            continue
        cid = str(raw_id).strip()
        if not cid:
            continue
        for col_idx, month in month_by_col.items():
            if _cell_has_lock_fill(ws.cell(row_idx, col_idx)):
                locks[(cid, month)] = True
    wb.close()
    return locks


def _apply_matrix_budgets(
    contracts: list[Contract],
    amounts: pd.DataFrame,
    locks: pd.DataFrame | None,
    year: int,
    *,
    fill_locks: dict[tuple[str, int], bool] | None = None,
) -> None:
    """Матрица: строки = проекты, столбцы = месяцы. Lock: заливка, * в ячейке или legacy fot_lock_matrix."""
    by_id = {c.id: c for c in contracts}
    id_col = amounts.columns[0]
    month_cols = _matrix_month_columns(amounts)
    if not month_cols:
        return

    lock_month_cols: dict[int, str] = {}
    if locks is not None and not locks.empty:
        lock_month_cols = _matrix_month_columns(locks)

    for _, row in amounts.iterrows():
        cid = str(row[id_col]).strip()
        if not cid or cid not in by_id:
            continue
        lock_row = None
        if locks is not None and not locks.empty:
            match = locks[locks.iloc[:, 0].astype(str).str.strip() == cid]
            lock_row = match.iloc[0] if len(match) else None

        for month, col in month_cols.items():
            val = row[col]
            amount, locked = _parse_matrix_cell_value(val)
            if amount is None:
                continue
            if fill_locks and fill_locks.get((cid, month)):
                locked = True
            if lock_row is not None and month in lock_month_cols:
                lock_val = lock_row[lock_month_cols[month]]
                locked = locked or _bool(lock_val)
            _upsert_month_inflow(by_id[cid], year, month, amount, lock=locked)


def _upsert_month_inflow(
    contract: Contract,
    year: int,
    month: int,
    inflow_amount: float,
    *,
    lock: bool = False,
) -> None:
    for mb in contract.monthly_budgets:
        if mb.month == month:
            mb.inflow_amount = inflow_amount
            mb.lock = lock
            return
    contract.monthly_budgets.append(
        ContractMonthlyBudget(
            contract_id=contract.id,
            year=year,
            month=month,
            inflow_amount=inflow_amount,
            lock=lock,
        )
    )


def _upsert_month_min_balance(
    contract: Contract, year: int, month: int, min_balance: float
) -> None:
    for mb in contract.monthly_budgets:
        if mb.month == month:
            mb.min_balance = min_balance
            return
    contract.monthly_budgets.append(
        ContractMonthlyBudget(
            contract_id=contract.id,
            year=year,
            month=month,
            inflow_amount=0.0,
            min_balance=min_balance,
        )
    )


def _apply_matrix_min_balances(contracts: list[Contract], df: pd.DataFrame, year: int) -> None:
    """Мин. остаток по месяцам: строки = проекты, столбцы = месяцы (как fot_matrix)."""
    by_id = {c.id: c for c in contracts}
    id_col = df.columns[0]
    month_cols = _matrix_month_columns(df)
    if not month_cols:
        return

    for _, row in df.iterrows():
        cid = str(row[id_col]).strip()
        if not cid or cid not in by_id:
            continue
        for month, col in month_cols.items():
            val = row[col]
            if val is None or (isinstance(val, float) and pd.isna(val)) or str(val).strip() == "":
                continue
            _upsert_month_min_balance(by_id[cid], year, month, float(val))


def _apply_budgets(contracts: list[Contract], df: pd.DataFrame) -> None:
    by_id = {c.id: c for c in contracts}
    touched: set[str] = set()
    for _, r in df.iterrows():
        cid = str(r["contract_id"]).strip()
        if cid not in by_id:
            continue
        touched.add(cid)
        inflow_raw = r.get("inflow_amount", 0)
        inflow = float(inflow_raw or 0)
        _upsert_month_inflow(
            by_id[cid],
            int(r["year"]),
            int(r["month"]),
            inflow,
            lock=_bool(r.get("lock")),
        )
        min_bal = _optional_float(r.get("min_balance"))
        if min_bal is not None:
            _upsert_month_min_balance(by_id[cid], int(r["year"]), int(r["month"]), min_bal)
    for cid in touched:
        by_id[cid].monthly_budgets.sort(key=lambda mb: mb.month)


def _load_manual_assignments(df: pd.DataFrame) -> list[ManualAssignment]:
    rows: list[ManualAssignment] = []
    for _, r in df.iterrows():
        fixed = r.get("fixed_amount")
        rows.append(
            ManualAssignment(
                employee_id=str(r["employee_id"]).strip(),
                contract_id=str(r["contract_id"]).strip(),
                year=int(r.get("year", date.today().year)),
                month_from=int(r["month_from"]),
                month_to=int(r["month_to"]),
                payment_kind=str(r.get("payment_kind", "salary")).strip().lower(),
                fixed_amount=float(fixed) if pd.notna(fixed) else None,
            )
        )
    return rows


def _load_manual_prohibitions(df: pd.DataFrame) -> list[ManualProhibition]:
    rows: list[ManualProhibition] = []
    for _, r in df.iterrows():
        pk = r.get("payment_kind")
        rows.append(
            ManualProhibition(
                employee_id=str(r["employee_id"]).strip(),
                contract_id=str(r["contract_id"]).strip(),
                year=int(r.get("year", date.today().year)),
                month_from=int(r.get("month_from", 1)),
                month_to=int(r.get("month_to", 12)),
                payment_kind=str(pk).strip().lower() if pd.notna(pk) else None,
            )
        )
    return rows


def _load_plan_overrides(df: pd.DataFrame, default_year: int) -> list[AllocationRecord]:
    """Читает лист plan: строки с lock=yes считаются ручными фиксациями для пересчёта."""
    records: list[AllocationRecord] = []
    for _, r in df.iterrows():
        locked = _bool(r.get("lock"), False)
        if not locked:
            continue
        records.append(
            AllocationRecord(
                employee_id=str(r["employee_id"]).strip(),
                contract_id=str(r["contract_id"]).strip(),
                year=int(r.get("year", default_year)),
                month=int(r["month"]),
                payment_kind=str(r["payment_kind"]).strip().lower(),
                amount=float(r["amount"]),
                is_manual=True,
                source="excel_lock",
            )
        )
    return records


def _load_contract_labor(
    df: pd.DataFrame,
    default_year: int,
    position_index: dict[str, PositionReferenceRow],
) -> list[ContractLaborPlan]:
    rows: list[ContractLaborPlan] = []
    for _, r in df.iterrows():
        cid = str(r["contract_id"]).strip()
        if not cid:
            continue
        pm = float(r["person_months"])
        if pm <= 0:
            continue
        pos_raw = r.get("position")
        position = str(pos_raw).strip() if pd.notna(pos_raw) and str(pos_raw).strip() else None
        ref = resolve_position(position, position_index) if position else None
        rows.append(
            ContractLaborPlan(
                contract_id=cid,
                year=int(r.get("year", default_year)),
                person_months=pm,
                position=position,
                equivalence_group=ref.группа_взаимозаменяемости if ref else None,
                position_level=ref.уровень if ref else None,
            )
        )
    return rows


def _load_salary_stability(settings: pd.DataFrame) -> SalaryStabilityRules:
    rules = SalaryStabilityRules()
    if settings.empty:
        return rules
    row = settings.iloc[0]
    if "max_salary_contracts_per_year" in row and pd.notna(row["max_salary_contracts_per_year"]):
        rules.max_contracts_per_year = int(row["max_salary_contracts_per_year"])
    if "min_fot_months_for_salary_reserve" in row and pd.notna(row["min_fot_months_for_salary_reserve"]):
        rules.min_fot_months_for_salary_reserve = int(row["min_fot_months_for_salary_reserve"])
    if "goz_labor_tolerance" in row and pd.notna(row["goz_labor_tolerance"]):
        rules.goz_labor_tolerance = float(row["goz_labor_tolerance"])
    return rules


def _load_weights(settings: pd.DataFrame) -> OptimizationWeights:
    w = OptimizationWeights()
    mapping = {
        "weight_uncovered_salary": "uncovered_salary",
        "weight_salary_switch": "salary_contract_switch",
        "weight_admin_complexity": "admin_complexity",
        "weight_flex_fragment": "flex_fragment",
        "weight_plan_deviation": "plan_deviation",
        "weight_uniform_spend_deviation": "uniform_spend_deviation",
        "weight_salary_compensation_via_flex": "salary_compensation_via_flex",
        "weight_labor_deviation": "labor_deviation",
    }
    legacy_admin_cols = (
        "weight_employee_contract_year_count",
        "weight_employee_contract_month_count",
        "weight_global_contract_count",
    )
    legacy_fragment_cols = (
        "weight_flex_payment_fragment_count",
        "weight_flex_fragment",
    )
    legacy_salary_switch_cols = (
        "weight_salary_switch",
        "weight_salary_contract_switch",
    )
    if settings.empty:
        return w
    row = settings.iloc[0]
    for col, attr in mapping.items():
        if col in row and pd.notna(row[col]):
            setattr(w, attr, float(row[col]))

    legacy_admin_vals = [
        float(row[col])
        for col in legacy_admin_cols
        if col in row and pd.notna(row[col])
    ]
    if "weight_admin_complexity" not in row or pd.isna(row.get("weight_admin_complexity")):
        if legacy_admin_vals:
            w.admin_complexity = max(legacy_admin_vals)

    if "weight_flex_fragment" not in row or pd.isna(row.get("weight_flex_fragment")):
        legacy_fragment_vals = [
            float(row[col])
            for col in legacy_fragment_cols
            if col in row and pd.notna(row[col])
        ]
        if legacy_fragment_vals:
            w.flex_fragment = max(legacy_fragment_vals)

    if "weight_salary_switch" not in row or pd.isna(row.get("weight_salary_switch")):
        for col in legacy_salary_switch_cols:
            if col in row and pd.notna(row[col]):
                w.salary_contract_switch = float(row[col])
                break

    return w


def _position_control_dataframe(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    employees = {e.id: e for e in ctx.employees}
    contracts = {c.id: c for c in ctx.contracts}
    rows: list[dict] = []

    for allocation in result.allocations:
        employee = employees.get(allocation.employee_id)
        contract = contracts.get(allocation.contract_id)
        if employee is None or contract is None:
            continue

        compatible_rules = []
        if contract.position_rules and employee.equivalence_group:
            compatible_rules = [
                pr
                for pr in contract.position_rules
                if pr.equivalence_group == employee.equivalence_group
            ]
        elif contract.position_rules:
            compatible_rules = [
                pr for pr in contract.position_rules if pr.position == employee.position
            ]

        if contract.position_rules:
            compatible = bool(compatible_rules)
        else:
            compatible = True

        if compatible_rules:
            salary_cap_1_rate = max(
                (pr.max_monthly_payment or pr.reference_salary_for_rate or 0.0)
                for pr in compatible_rules
            )
            contract_positions = "; ".join(sorted({pr.position for pr in compatible_rules}))
            contract_groups = "; ".join(
                sorted({pr.equivalence_group or "" for pr in compatible_rules if pr.equivalence_group})
            )
        else:
            salary_cap_1_rate = None
            contract_positions = ""
            contract_groups = ""

        rows.append(
            {
                "сотрудник": allocation.employee_id,
                "фио": employee.full_name,
                "должность сотрудника": employee.position,
                "группа сотрудника": employee.equivalence_group or "",
                "договор": allocation.contract_id,
                "должность по договору": contract_positions,
                "группа по договору": contract_groups,
                "месяц": RU_MONTHS.get(allocation.month, allocation.month),
                "вид выплаты": _PAYMENT_KIND_RU.get(
                    allocation.payment_kind, allocation.payment_kind
                ),
                "сумма": allocation.amount,
                "совместимость": "да" if compatible else "нет",
                "окладный потолок за 1 ставку": salary_cap_1_rate if salary_cap_1_rate is not None else "",
                "ставка": employee.rate,
                "потолок с учетом ставки": (
                    salary_cap_1_rate * employee.rate
                    if salary_cap_1_rate is not None
                    else ""
                ),
            }
        )

    return pd.DataFrame(rows)


def _labor_by_group_dataframe(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    employees = {e.id: e for e in ctx.employees}
    facts: dict[tuple[str, str | None], float] = {}
    for allocation in result.allocations:
        if allocation.payment_kind != "salary" or allocation.amount < 0.01:
            continue
        employee = employees.get(allocation.employee_id)
        if employee is None:
            continue
        key = (allocation.contract_id, employee.equivalence_group)
        facts[key] = facts.get(key, 0.0) + employee.rate

    rows: list[dict] = []
    for contract in ctx.contracts:
        for group, plan_pm in planned_labor_groups(ctx, contract.id):
            fact_pm = facts.get((contract.id, group), 0.0)
            deviation = fact_pm - plan_pm
            rows.append(
                {
                    "договор": contract.id,
                    "группа взаимозаменяемости": group or "",
                    "план чел.-мес.": round(plan_pm, 4),
                    "факт чел.-мес.": round(fact_pm, 4),
                    "отклонение": round(deviation, 4),
                    "статус": "выполнено" if abs(deviation) <= 0.01 else "отклонение",
                }
            )

    return pd.DataFrame(rows)


def _project_monthly_grid(ctx: PlanningContext, result: PlanningResult) -> pd.DataFrame:
    """Остаток лимита ФОТ договора после плана выплат (не кассовый остаток)."""
    planned_by_contract_month: dict[tuple[str, int], float] = {}
    for allocation in result.allocations:
        key = (allocation.contract_id, allocation.month)
        planned_by_contract_month[key] = planned_by_contract_month.get(key, 0.0) + allocation.amount

    rows = []
    short_year = str(result.year)[-2:]
    fot_remainder_label = "Ост. лимита ФОТ"
    for contract in ctx.contracts:
        remaining_fot = contract.total_fot
        row = {
            "код": contract.id,
            "проект": contract.name,
            "тип договора": contract.contract_type,
        }
        for month in range(1, 13):
            if month == 1:
                row[f"{fot_remainder_label} на 01.01.{result.year}"] = remaining_fot
            month_name = RU_MONTHS[month]
            month_plan = planned_by_contract_month.get((contract.id, month), 0.0)
            row[f"{month_name} {short_year} (план)"] = month_plan
            remaining_fot -= month_plan
            next_month = month + 1
            next_year = result.year
            if next_month == 13:
                next_month = 1
                next_year += 1
            row[f"{fot_remainder_label} на 01.{next_month:02d}.{next_year}"] = remaining_fot
        rows.append(row)
    return pd.DataFrame(rows)


def _result_readable_tables(
    ctx: PlanningContext, result: PlanningResult
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    employees = {e.id: e for e in ctx.employees}
    contracts = {c.id: c for c in ctx.contracts}
    outgoing_transfers: dict[tuple[str, int], list[str]] = {}
    incoming_transfers: dict[tuple[str, int], list[str]] = {}
    for transfer in result.month_transfers:
        outgoing_transfers.setdefault((transfer.contract_id, transfer.from_month), []).append(
            f"{RU_MONTHS[transfer.to_month]}: {transfer.amount:,.0f}".replace(",", " ")
        )
        incoming_transfers.setdefault((transfer.contract_id, transfer.to_month), []).append(
            f"{RU_MONTHS[transfer.from_month]}: {transfer.amount:,.0f}".replace(",", " ")
        )

    plan_rows = []
    for allocation in result.allocations:
        employee = employees.get(allocation.employee_id)
        contract = contracts.get(allocation.contract_id)
        plan_rows.append(
            {
                "табельный номер": allocation.employee_id,
                "фио": employee.full_name if employee else "",
                "должность": employee.position if employee else "",
                "договор": allocation.contract_id,
                "проект": contract.name if contract else "",
                "год": allocation.year,
                "месяц": RU_MONTHS[allocation.month],
                "вид выплаты": _PAYMENT_KIND_RU.get(
                    allocation.payment_kind, allocation.payment_kind
                ),
                "сумма": allocation.amount,
                "зафиксировано": "да" if allocation.is_manual else "нет",
                "источник": allocation.source,
            }
        )

    balance_rows = []
    for balance in result.contract_balances:
        contract = contracts.get(balance.contract_id)
        balance_rows.append(
            {
                "договор": balance.contract_id,
                "проект": contract.name if contract else "",
                "год": balance.year,
                "месяц": RU_MONTHS[balance.month],
                "остаток на начало": balance.opening_balance,
                "поступление": balance.inflow,
                "потрачено": balance.spent,
                "остаток на конец": balance.closing_balance,
                "перенос на будущий месяц": balance.carried_forward,
                "резерв оклада": balance.salary_reserve_required,
                "неподвижный остаток": balance.min_balance_required,
                "можно перенести назад": balance.movable_balance,
                "перенос пришел": balance.transfer_in,
                "откуда пришло": "; ".join(incoming_transfers.get((balance.contract_id, balance.month), [])),
                "перенос ушел": balance.transfer_out,
                "куда перенесено": "; ".join(outgoing_transfers.get((balance.contract_id, balance.month), [])),
            }
        )

    transfer_rows = []
    matrix_rows: dict[tuple[str, int], dict] = {}
    for transfer in result.month_transfers:
        contract = contracts.get(transfer.contract_id)
        transfer_rows.append(
            {
                "договор": transfer.contract_id,
                "проект": contract.name if contract else "",
                "из месяца": RU_MONTHS[transfer.from_month],
                "в месяц": RU_MONTHS[transfer.to_month],
                "сумма": transfer.amount,
            }
        )
        key = (transfer.contract_id, transfer.from_month)
        row = matrix_rows.setdefault(
            key,
            {
                "договор": transfer.contract_id,
                "проект": contract.name if contract else "",
                "из месяца": RU_MONTHS[transfer.from_month],
            },
        )
        col = f"в {RU_MONTHS[transfer.to_month]}"
        row[col] = row.get(col, 0.0) + transfer.amount

    month_cols = [f"в {RU_MONTHS[month]}" for month in range(1, 13)]
    matrix_df = pd.DataFrame(matrix_rows.values())
    if matrix_df.empty:
        matrix_df = pd.DataFrame(columns=["договор", "проект", "из месяца", *month_cols])
    else:
        for col in month_cols:
            if col not in matrix_df.columns:
                matrix_df[col] = 0.0
        matrix_df = matrix_df[["договор", "проект", "из месяца", *month_cols]]

    transfers_df = pd.DataFrame(transfer_rows)
    if transfers_df.empty:
        transfers_df = pd.DataFrame(columns=["договор", "проект", "из месяца", "в месяц", "сумма"])

    return pd.DataFrame(plan_rows), pd.DataFrame(balance_rows), transfers_df, matrix_df


_BALANCE_EXPORT_COLUMNS = [
    "договор",
    "проект",
    "год",
    "месяц",
    "остаток на начало",
    "потрачено",
    "остаток на конец",
    "перенос пришел",
    "откуда пришло",
    "перенос ушел",
    "куда перенесено",
]


def _export_balances_sheet(writer: pd.ExcelWriter, balances_df: pd.DataFrame) -> None:
    if balances_df.empty:
        pd.DataFrame(columns=_BALANCE_EXPORT_COLUMNS).to_excel(
            writer, sheet_name="остатки_и_переносы", index=False
        )
        return
    balances_df[_BALANCE_EXPORT_COLUMNS].to_excel(
        writer, sheet_name="остатки_и_переносы", index=False
    )


def _format_workbook(path: Path, *, result: bool = False) -> None:
    """Оформление входного или результирующего Excel: шапка, ширина, числа."""
    from openpyxl import load_workbook
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = load_workbook(path)
    header_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    money_headers = (
        "лимита ФОТ",
        "(план)",
        "сумма",
        "остаток",
        "поступление",
        "потрачено",
        "перенос",
        "резерв",
        "фот",
        "оклад",
        "надбав",
        "план",
        "факт",
        "отклон",
        "накоплен",
        "amount",
        "balance",
        "inflow",
        "spent",
    )

    for ws in wb.worksheets:
        if ws.max_row < 1:
            continue
        ws.freeze_panes = "A2"
        if ws.max_row > 1:
            ws.auto_filter.ref = ws.dimensions
        ws.sheet_view.showGridLines = False

        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.row_dimensions[1].height = 30

        for column in ws.columns:
            header = str(column[0].value or "")
            letter = get_column_letter(column[0].column)
            width = max(len(str(cell.value or "")) for cell in column)
            ws.column_dimensions[letter].width = min(max(width + 2, 10), 28)
            if any(token in header.lower() for token in money_headers):
                for cell in column[1:]:
                    cell.number_format = "#,##0"

    if result:
        _format_result_workbook_sheets(wb)
    else:
        if SHEET_FOT_MATRIX in wb.sheetnames:
            ws = wb[SHEET_FOT_MATRIX]
            ws.freeze_panes = "B2"
            for col in range(2, ws.max_column + 1):
                if _month_from_column(ws.cell(1, col).value) == 12 and ws.max_row >= 2:
                    ws.cell(2, col).fill = PatternFill(fill_type="solid", fgColor="FFFF00")
                    break

    wb.save(path)


def _format_result_workbook(path: Path) -> None:
    from openpyxl import load_workbook

    wb = load_workbook(path)
    _format_result_workbook_sheets(wb)
    wb.save(path)


def _format_result_workbook_sheets(wb) -> None:
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Alignment
    from openpyxl.utils import get_column_letter

    if "проекты_помесячно" in wb.sheetnames:
        ws = wb["проекты_помесячно"]
        ws.freeze_panes = "D2"
        ws.sheet_properties.tabColor = "70AD47"
        ws.column_dimensions["A"].width = 10
        ws.column_dimensions["B"].width = 24
        ws.column_dimensions["C"].width = 14
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top")

    if "переносы_матрица" in wb.sheetnames:
        ws = wb["переносы_матрица"]
        ws.freeze_panes = "D2"
        ws.sheet_properties.tabColor = "FFC000"
        if ws.max_row > 1 and ws.max_column > 3:
            start = get_column_letter(4)
            end = get_column_letter(ws.max_column)
            ws.conditional_formatting.add(
                f"{start}2:{end}{ws.max_row}",
                ColorScaleRule(
                    start_type="min",
                    start_color="FFFFFF",
                    end_type="max",
                    end_color="F4B183",
                ),
            )


def export_result(path: str | Path, ctx: PlanningContext, result: PlanningResult) -> None:
    path = Path(path)
    budget_fixed_rows = []
    for c in ctx.contracts:
        for mb in c.monthly_budgets:
            if mb.lock:
                budget_fixed_rows.append(
                    {
                        "договор": c.id,
                        "год": mb.year,
                        "месяц": RU_MONTHS[mb.month],
                        "поступление": mb.inflow_amount,
                        "фикс": "да",
                    }
                )
    budget_fixed_df = pd.DataFrame(budget_fixed_rows)
    project_grid_df = _project_monthly_grid(ctx, result)
    readable_plan_df, readable_balances_df, readable_transfers_df, transfer_matrix_df = (
        _result_readable_tables(ctx, result)
    )
    readable_deficits_df = pd.DataFrame(
        [
            {
                "табельный номер": d.employee_id,
                "год": d.year,
                "месяц": RU_MONTHS[d.month],
                "вид выплаты": _PAYMENT_KIND_RU.get(d.payment_kind, d.payment_kind),
                "сумма": d.amount,
                "причины": "; ".join(d.reasons),
            }
            for d in result.deficits
        ]
    )
    readable_conflicts_df = pd.DataFrame(
        [
            {
                "код": c.code,
                "сообщение": c.message,
                "табельный номер": c.employee_id,
                "договор": c.contract_id,
                "год": c.year,
                "месяц": RU_MONTHS[c.month] if c.month else "",
            }
            for c in result.conflicts
        ]
    )
    from fot_planner.spend_plan import build_spend_plan_fact_dataframe

    spend_plan_fact_df = build_spend_plan_fact_dataframe(ctx, result)
    readable_meta_df = pd.DataFrame(
        [
            {
                "статус решателя": result.solver_status,
                "целевая функция": result.objective_value,
                "время расчета, сек": result.solve_time_sec,
                "год": result.year,
            }
        ]
    )
    position_control_df = _position_control_dataframe(ctx, result)
    labor_by_group_df = _labor_by_group_dataframe(ctx, result)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        project_grid_df.to_excel(writer, sheet_name="проекты_помесячно", index=False)
        readable_plan_df.to_excel(writer, sheet_name="план_выплат", index=False)
        spend_plan_fact_df.to_excel(writer, sheet_name="освоение_план_факт", index=False)
        _export_balances_sheet(writer, readable_balances_df)
        readable_transfers_df.to_excel(writer, sheet_name="переносы", index=False)
        if not transfer_matrix_df.empty:
            transfer_matrix_df.to_excel(writer, sheet_name="переносы_матрица", index=False)
        readable_deficits_df.to_excel(writer, sheet_name="дефициты", index=False)
        if not readable_conflicts_df.empty:
            readable_conflicts_df.to_excel(writer, sheet_name="конфликты", index=False)
        readable_meta_df.to_excel(writer, sheet_name="расчет", index=False)
        if not budget_fixed_df.empty:
            budget_fixed_df.to_excel(writer, sheet_name="фиксированный_фот", index=False)
        position_control_df.to_excel(writer, sheet_name="контроль_должностей", index=False)
        labor_by_group_df.to_excel(writer, sheet_name="трудоемкость_по_группам", index=False)
    _format_workbook(path, result=True)


def create_template(path: str | Path) -> None:
    path = Path(path)
    year = date.today().year

    employees = pd.DataFrame(
        [
            {
                "код": "E001",
                "фио": "Иванов Иван Иванович",
                "должность": "инженер",
                "подразделение": "лаборатория",
                "ставка": 1.0,
                "оклад": 100000,
                "надбавка": 0,
                "стимулирующая": 0,
                "дата начала": f"{year}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "",
                "запрещенные договоры": "",
            }
        ]
    )
    contract_types = pd.DataFrame(
        [
            {
                "code": "goszakaz",
                "name": "Государственный заказ",
                "allow_monthly_carryover": False,
                "allow_use_after_end": False,
                "months_after_end": 0,
                "spend_complete_days_before_end": 20,
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            },
            {
                "code": "grant",
                "name": "Грант",
                "allow_monthly_carryover": True,
                "allow_use_after_end": False,
                "months_after_end": 0,
                "spend_complete_days_before_end": "",
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            },
            {
                "code": "minprom",
                "name": "Минпромторг",
                "allow_monthly_carryover": True,
                "allow_use_after_end": False,
                "months_after_end": 0,
                "spend_complete_days_before_end": "",
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            },
            {
                "code": "off_budget",
                "name": "Внебюджет",
                "allow_monthly_carryover": True,
                "allow_use_after_end": True,
                "months_after_end": 2,
                "spend_complete_days_before_end": "",
                "allow_salary": True,
                "allow_allowance": True,
                "allow_incentive": True,
            },
        ]
    )
    contract_labor = pd.DataFrame(columns=["договор", "год", "трудоемкость", "должность"])
    contracts = pd.DataFrame(
        [
            {
                "код": "C001",
                "название": "НИОКР Альфа",
                "номер": "123/2025",
                "тип договора": "goszakaz",
                "дата начала": f"{year}-01-01",
                "дата окончания": f"{year}-12-31",
                "срок освоения": f"{year}-12-11",
                "фот": 1_200_000,
                "оклад разрешен": True,
                "надбавка разрешена": True,
                "стимулирующая разрешена": True,
                "месяцев после окончания": 0,
                "перенос остатков": True,
                "резерв оклада": False,
            }
        ]
    )
    positions = pd.DataFrame(
        [
            {
                "договор": "C001",
                "должность": "инженер",
                "макс ставки": 2,
                "макс выплата": 120000,
            }
        ]
    )
    min_balance_matrix = pd.DataFrame({"договор": ["C001"]})
    for m in range(1, 13):
        min_balance_matrix[str(m)] = [""]

    settings = pd.DataFrame(
        [
            {
                "год": year,
                "штраф дефицита": 1_000_000,
                "макс договоров оклада в год": 2,
                "штраф смены договора оклада": 500_000,
                "штраф административной сложности выплат": 200_000,
                "штраф дробления переменных выплат": 200_000,
                "месяцев фот для резерва": 6,
                "допуск трудоемкости гоз": 0.05,
            }
        ]
    )
    manual_assignments = pd.DataFrame(
        columns=[
            "сотрудник",
            "договор",
            "год",
            "месяц с",
            "месяц по",
            "вид выплаты",
            "фикс сумма",
        ]
    )
    manual_prohibitions = pd.DataFrame(
        columns=["сотрудник", "договор", "год", "месяц с", "месяц по", "вид выплаты"]
    )

    readme = pd.DataFrame(
        [
            {"лист": SHEET_EMPLOYEES, "описание": "Справочник сотрудников"},
            {"лист": SHEET_CONTRACTS, "описание": "Договоры"},
            {"лист": SHEET_CONTRACT_POSITIONS, "описание": "Допустимые должности и лимиты"},
            {
                "лист": SHEET_FOT_MATRIX,
                "описание": "(опционально) переопределить поступления; иначе ФОТ/сроки с contracts",
            },
            {
                "лист": SHEET_MIN_BALANCE_MATRIX,
                "описание": "Неподвижные: не отдавать на более ранние месяцы больше (пул − сумма)",
            },
            {
                "лист": SHEET_FOT_LOCK_MATRIX,
                "описание": "(устар.) отдельная сетка yes — только если без заливки/*",
            },
            {
                "лист": SHEET_CONTRACT_BUDGET,
                "описание": "Альтернатива: длинный список (contract_id, month, inflow_amount, lock)",
            },
            {
                "лист": SHEET_CONTRACT_LABOR,
                "описание": "Трудоёмкость (чел.-мес.); должность опциональна; ГОЗ ±goz_labor_tolerance",
            },
            {"лист": SHEET_MANUAL_ASSIGNMENTS, "описание": "Ручные фиксации (fixed_amount пусто = только привязка)"},
            {"лист": "plan (результат)", "описание": "lock=yes — зафиксировать строку при пересчёте"},
        ]
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        readme.to_excel(writer, sheet_name="readme", index=False)
        settings.to_excel(writer, sheet_name=SHEET_SETTINGS, index=False)
        employees.to_excel(writer, sheet_name=SHEET_EMPLOYEES, index=False)
        contracts.to_excel(writer, sheet_name=SHEET_CONTRACTS, index=False)
        positions.to_excel(writer, sheet_name=SHEET_CONTRACT_POSITIONS, index=False)
        contract_labor.to_excel(writer, sheet_name=SHEET_CONTRACT_LABOR, index=False)
        min_balance_matrix.to_excel(writer, sheet_name=SHEET_MIN_BALANCE_MATRIX, index=False)
        manual_assignments.to_excel(writer, sheet_name=SHEET_MANUAL_ASSIGNMENTS, index=False)
        manual_prohibitions.to_excel(writer, sheet_name=SHEET_MANUAL_PROHIBITIONS, index=False)
    _format_workbook(path, result=False)


def _mark_template_locked_fot_cells(path: Path) -> None:
    """В шаблоне декабрь C001 — жёлтая заливка = пример фиксации."""
    from openpyxl import load_workbook
    from openpyxl.styles import PatternFill

    wb = load_workbook(path)
    ws = wb[SHEET_FOT_MATRIX]
    yellow = PatternFill(fill_type="solid", fgColor="FFFF00")
    for col in range(2, ws.max_column + 1):
        if _month_from_column(ws.cell(1, col).value) == 12:
            ws.cell(2, col).fill = yellow
            break
    wb.save(path)
