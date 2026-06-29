from __future__ import annotations

import pandas as pd

from fot_planner.excel.constants import SHEET_EMPLOYEES, SHEET_MANUAL_ASSIGNMENTS
from fot_planner.excel.parsing import _canonicalize_columns


def test_same_column_name_can_mean_different_fields_on_different_sheets():
    df = pd.DataFrame(columns=["сотрудник", "договор", "год"])

    employee_columns = _canonicalize_columns(df, SHEET_EMPLOYEES).columns
    manual_columns = _canonicalize_columns(df, SHEET_MANUAL_ASSIGNMENTS).columns

    assert list(employee_columns) == ["full_name", "договор", "год"]
    assert list(manual_columns) == ["employee_id", "contract_id", "year"]
