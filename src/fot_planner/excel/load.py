"""Загрузка PlanningContext из Excel."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from fot_planner.defaults.contract_types import (
    CONTRACT_TYPE_DEFAULTS,
    GENERIC_CONTRACT_TYPE_DEFAULT,
    normalize_contract_type,
)
from fot_planner.excel.constants import (
    SHEET_CONTRACT_BUDGET,
    SHEET_CONTRACT_LABOR,
    SHEET_CONTRACT_POSITIONS,
    SHEET_CONTRACT_TYPES,
    SHEET_CONTRACTS,
    SHEET_EMPLOYEES,
    SHEET_FOT_LOCK_MATRIX,
    SHEET_FOT_MATRIX,
    SHEET_MANUAL_ASSIGNMENTS,
    SHEET_MANUAL_PROHIBITIONS,
    SHEET_META,
    SHEET_MIN_BALANCE_MATRIX,
    SHEET_PLAN,
    SHEET_POSITION_REFERENCE,
    SHEET_POSITION_SALARY_LIMITS,
    SHEET_POSITION_SYNONYMS,
    SHEET_SETTINGS,
)
from fot_planner.excel.parsing import (
    _bool,
    _canonicalize_columns,
    _canonicalize_manual_columns,
    _optional_bool,
    _optional_float,
    _optional_int,
    _parse_date,
    _resolve_monthly_wage,
    _split_list,
    parse_payment_kind,
)
from fot_planner.fot_schedule import default_fot_inflow_at_start
from fot_planner.open_rate_rules import normalize_employment_category
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
    PositionReference,
    SalaryStabilityRules,
    labor_row_id,
)
from fot_planner.position_reference import (
    PositionReferenceRow,
    build_position_index,
    default_position_reference,
    default_position_synonyms,
    normalize_position,
    resolve_position,
)
from fot_planner.salary_limits_2556 import (
    PositionSalaryLimit,
    default_position_salary_limits,
    p4_applies_to_category,
)

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
            position=row.position,
            equivalence_group=row.equivalence_group,
            level=row.level,
            reference_salary_for_rate=row.reference_salary_for_rate,
        )
        for row in position_reference_rows
    ]
    position_salary_limits = _load_position_salary_limits(xl)

    employees = _load_employees(
        _canonicalize_columns(pd.read_excel(xl, SHEET_EMPLOYEES)),
        position_index,
    )
    contract_type_defaults = _load_contract_type_defaults(xl)
    contracts = _load_contracts(
        _canonicalize_columns(pd.read_excel(xl, SHEET_CONTRACTS)),
        contract_type_defaults,
    )

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
        for c in contracts:
            c.monthly_budgets = []
        _apply_matrix_budgets(
            contracts,
            amounts_df,
            lock_df,
            year,
            fill_locks=fill_locks,
        )
    elif SHEET_CONTRACT_BUDGET in xl.sheet_names:
        for c in contracts:
            c.monthly_budgets = []
        _apply_budgets(
            contracts,
            _canonicalize_columns(pd.read_excel(xl, SHEET_CONTRACT_BUDGET)),
        )
    else:
        default_fot_inflow_at_start(contracts, year)

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
    allow_deficit = False
    if settings_df is not None:
        weights = _load_weights(settings_df)
        salary_stability = _load_salary_stability(settings_df)
        if not settings_df.empty:
            row = settings_df.iloc[0]
            if "allow_deficit" in row and pd.notna(row["allow_deficit"]):
                allow_deficit = _bool(row["allow_deficit"])

    return PlanningContext(
        year=year,
        employees=employees,
        contracts=contracts,
        position_reference=position_reference,
        position_salary_limits=position_salary_limits,
        manual_assignments=manual_assignments,
        manual_prohibitions=manual_prohibitions,
        weights=weights,
        salary_stability=salary_stability,
        labor_plans=labor_plans,
        baseline_plan=baseline_plan,
        allow_deficit=allow_deficit,
    )


def _clean_optional_text(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _load_position_salary_limits(xl: pd.ExcelFile) -> list[PositionSalaryLimit]:
    """Лист заменяет встроенный справочник целиком, включая изменённые суммы."""
    if SHEET_POSITION_SALARY_LIMITS not in xl.sheet_names:
        return default_position_salary_limits()

    df = _canonicalize_columns(pd.read_excel(xl, SHEET_POSITION_SALARY_LIMITS))
    rows: list[PositionSalaryLimit] = []
    for _, r in df.iterrows():
        position = _clean_optional_text(r.get("position"))
        personnel_category = _clean_optional_text(r.get("personnel_category"))
        if not position or not personnel_category:
            continue
        rows.append(
            PositionSalaryLimit(
                position=position,
                personnel_category=personnel_category,
                order_2556_limit=_optional_float(r.get("order_2556_limit")),
                p4_limit=_optional_float(r.get("p4_limit")),
                note=_clean_optional_text(r.get("salary_limit_note")),
            )
        )
    return rows


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
                position=raw_position,
                equivalence_group=raw_group,
                level=_optional_int(r.get("position_level")),
                reference_salary_for_rate=_optional_float(
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
                monthly_wage=_resolve_monthly_wage(r),
                start_date=_parse_date(r.get("start_date")),
                end_date=_parse_date(r.get("end_date")),
                allowed_contracts=_split_list(r.get("allowed_contracts")),
                forbidden_contracts=_split_list(r.get("forbidden_contracts")),
                equivalence_group=ref.equivalence_group if ref else None,
                position_level=ref.level if ref else None,
                reference_salary_for_rate=ref.reference_salary_for_rate if ref else None,
                employment_category=normalize_employment_category(
                    r.get("employment_category")
                ),
            )
        )
    return rows


_CONTRACT_TYPE_BOOL_FIELDS = (
    "allow_salary",
    "allow_secret",
    "allow_allowance",
    "allow_incentive",
    "allow_extra_work",
    "allow_order_incentive",
    "allowance_requires_salary_contract",
    "priority_payment_mode",
)
_CONTRACT_TYPE_TEXT_FIELDS = (
    "staff_limit_sources",
    "salary_allowance_limit_sources",
)
_CONTRACT_TYPE_FLOAT_FIELDS = (
    "agreement_staff_limit",
    "secret_rate",
)
_CONTRACT_TYPE_INT_FIELDS = (
    "salary_anchor_priority",
)


def _has_value(value: object) -> bool:
    return value is not None and not pd.isna(value) and str(value).strip() != ""


def _load_contract_type_defaults(xl: pd.ExcelFile) -> dict[str, dict[str, object]]:
    defaults = {key: dict(value) for key, value in CONTRACT_TYPE_DEFAULTS.items()}
    if SHEET_CONTRACT_TYPES not in xl.sheet_names:
        return defaults

    df = _canonicalize_columns(pd.read_excel(xl, SHEET_CONTRACT_TYPES))
    for _, r in df.iterrows():
        raw_code = r.get("code", r.get("contract_type", r.get("id")))
        if not _has_value(raw_code):
            continue
        code = normalize_contract_type(raw_code)
        row_defaults = dict(defaults.get(code, {}))
        for field_name in _CONTRACT_TYPE_BOOL_FIELDS:
            if field_name in r.index and _has_value(r.get(field_name)):
                row_defaults[field_name] = _bool(r.get(field_name))
        for field_name in _CONTRACT_TYPE_TEXT_FIELDS:
            if field_name in r.index and _has_value(r.get(field_name)):
                row_defaults[field_name] = str(r.get(field_name)).strip()
        for field_name in _CONTRACT_TYPE_FLOAT_FIELDS:
            if field_name in r.index and _has_value(r.get(field_name)):
                row_defaults[field_name] = float(r.get(field_name))
        for field_name in _CONTRACT_TYPE_INT_FIELDS:
            if field_name in r.index and _has_value(r.get(field_name)):
                row_defaults[field_name] = int(float(r.get(field_name)))
        defaults[code] = row_defaults
    return defaults


def _type_defaults(
    contract_type: str, contract_type_defaults: dict[str, dict[str, object]]
) -> dict[str, object]:
    generic = dict(GENERIC_CONTRACT_TYPE_DEFAULT)
    known = contract_type_defaults.get(contract_type)
    if known is None:
        return generic
    return {**generic, **known}


def _resolve_is_goz_defense_order(contract_type: str, staff_limit_sources: str) -> bool:
    if contract_type == "goz":
        return True
    return "bep" in staff_limit_sources.lower()


def _optional_deadline(row: pd.Series, column: str) -> date | None:
    if column not in row.index or not _has_value(row.get(column)):
        return None
    return _parse_date(row.get(column))


def _legacy_allowances_payment_deadline(row: pd.Series) -> date | None:
    """Старые файлы: отдельные колонки по 120/122/124/152 — берём самую позднюю дату."""
    legacy_cols = (
        "allowances_payment_deadline",
        "flex_payment_deadline",
        "allowance_payment_deadline",
        "incentive_payment_deadline",
        "secret_payment_deadline",
        "extra_work_payment_deadline",
    )
    dates = [d for col in legacy_cols if (d := _optional_deadline(row, col)) is not None]
    return max(dates) if dates else None


def _parse_contract_payment_deadlines(row: pd.Series) -> tuple[date | None, date | None]:
    return (
        _optional_deadline(row, "salary_payment_deadline"),
        _legacy_allowances_payment_deadline(row),
    )


def _apply_payment_deadlines(contract: Contract) -> None:
    """Пустые даты в Excel → дата окончания договора (в срок)."""
    if contract.salary_payment_deadline is None:
        contract.salary_payment_deadline = contract.end_date
    if contract.allowances_payment_deadline is None:
        contract.allowances_payment_deadline = contract.end_date


def _load_contracts(
    df: pd.DataFrame, contract_type_defaults: dict[str, dict[str, object]]
) -> list[Contract]:
    rows: list[Contract] = []
    for _, r in df.iterrows():
        ctype = normalize_contract_type(r["contract_type"])
        defaults = _type_defaults(ctype, contract_type_defaults)
        staff_limit_sources = (
            str(r.get("staff_limit_sources")).strip()
            if "staff_limit_sources" in r.index and _has_value(r.get("staff_limit_sources"))
            else str(defaults.get("staff_limit_sources", "") or "")
        )
        is_goz = _resolve_is_goz_defense_order(ctype, staff_limit_sources)
        salary_payment_deadline, allowances_payment_deadline = _parse_contract_payment_deadlines(r)
        rows.append(
            Contract(
                id=str(r["id"]).strip(),
                name=str(r.get("name", "")).strip(),
                number=str(r.get("number", "")).strip(),
                contract_type=ctype,
                start_date=_parse_date(r["start_date"]),
                end_date=_parse_date(r["end_date"]),
                total_fot=float(r["total_fot"]),
                is_goz_defense_order=is_goz,
                allow_salary=_bool(r.get("allow_salary"), bool(defaults["allow_salary"])),
                allow_secret=_bool(
                    r.get("allow_secret"), bool(defaults["allow_secret"])
                ),
                allow_allowance=_bool(
                    r.get("allow_allowance"), bool(defaults["allow_allowance"])
                ),
                allow_incentive=_bool(
                    r.get("allow_incentive"), bool(defaults["allow_incentive"])
                ),
                allow_extra_work=_bool(
                    r.get("allow_extra_work"), bool(defaults["allow_extra_work"])
                ),
                allow_order_incentive=_bool(
                    r.get("allow_order_incentive"),
                    bool(defaults.get("allow_order_incentive", False)),
                ),
                staff_limit_sources=staff_limit_sources,
                salary_allowance_limit_sources=(
                    str(r.get("salary_allowance_limit_sources")).strip()
                    if "salary_allowance_limit_sources" in r.index
                    and _has_value(r.get("salary_allowance_limit_sources"))
                    else str(defaults.get("salary_allowance_limit_sources", "") or "")
                ),
                agreement_staff_limit=(
                    _optional_float(r.get("agreement_staff_limit"))
                    if "agreement_staff_limit" in r.index
                    and _has_value(r.get("agreement_staff_limit"))
                    else defaults.get("agreement_staff_limit")
                ),
                allowance_requires_salary_contract=_bool(
                    r.get("allowance_requires_salary_contract"),
                    bool(defaults.get("allowance_requires_salary_contract", True)),
                ),
                secret_rate=(
                    _optional_float(r.get("secret_rate"))
                    if "secret_rate" in r.index and _has_value(r.get("secret_rate"))
                    else float(defaults.get("secret_rate", 0.05) or 0.05)
                ),
                priority_payment_mode=_bool(
                    r.get("priority_payment_mode"),
                    bool(defaults.get("priority_payment_mode", False)),
                ),
                salary_anchor_priority=(
                    int(float(r.get("salary_anchor_priority")))
                    if "salary_anchor_priority" in r.index
                    and _has_value(r.get("salary_anchor_priority"))
                    else int(defaults.get("salary_anchor_priority", 0) or 0)
                ),
                salary_payment_deadline=salary_payment_deadline,
                allowances_payment_deadline=allowances_payment_deadline,
                allow_main_employment=_bool(r.get("allow_main_employment"), True),
                allow_part_time=_bool(r.get("allow_part_time"), True),
            )
        )
        _apply_payment_deadlines(rows[-1])
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

        by_id[cid].position_rules.append(
            ContractPositionRule(
                contract_id=cid,
                position=position,
                max_monthly_payment=None,
                max_positions=_optional_float(r.get("max_positions")),
                equivalence_group=ref.equivalence_group if ref else None,
                position_level=ref.level if ref else None,
                reference_salary_for_rate=ref.reference_salary_for_rate if ref else None,
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
    """Матрица: строки = проекты, столбцы = месяцы. Lock: заливка, * в ячейке или fot_lock_matrix."""
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
                payment_kind=parse_payment_kind(r.get("payment_kind")),
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
                payment_kind=parse_payment_kind(pk) if pd.notna(pk) else None,
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
                payment_kind=parse_payment_kind(r["payment_kind"]),
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
        avg_raw = r.get("avg_monthly_labor_cost")
        avg_cost = float(avg_raw) if pd.notna(avg_raw) and str(avg_raw).strip() else None
        rows.append(
            ContractLaborPlan(
                contract_id=cid,
                year=int(r.get("year", default_year)),
                person_months=pm,
                position=position,
                equivalence_group=ref.equivalence_group if ref else None,
                position_level=ref.level if ref else None,
                avg_monthly_labor_cost=avg_cost,
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
    if "goz_labor_tolerance" in row and pd.notna(row["goz_labor_tolerance"]):
        rules.goz_labor_tolerance = float(row["goz_labor_tolerance"])
    if "goz_average_salary_limit" in row and pd.notna(row["goz_average_salary_limit"]):
        rules.goz_average_salary_limit = float(row["goz_average_salary_limit"])
    # Открытые ставки всегда включены; столбец в Excel не управляет расчетом.
    rules.enable_open_rates = True
    return rules


def _load_weights(settings: pd.DataFrame) -> OptimizationWeights:
    w = OptimizationWeights()
    mapping = {
        "weight_salary_switch": "salary_contract_switch",
        "weight_admin_complexity": "admin_complexity",
        "weight_plan_deviation": "plan_deviation",
        "weight_uniform_spend_deviation": "uniform_spend_deviation",
        "weight_labor_deviation": "labor_deviation",
        "weight_rate_below_staff": "rate_below_staff",
    }
    if settings.empty:
        return w
    row = settings.iloc[0]
    for col, attr in mapping.items():
        if col in row and pd.notna(row[col]):
            setattr(w, attr, float(row[col]))

    return w

