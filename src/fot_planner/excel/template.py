"""Шаблон входного Excel."""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import PatternFill

from fot_planner.excel.constants import (
    SHEET_CONTRACT_LABOR,
    SHEET_CONTRACTS,
    SHEET_EMPLOYEES,
    SHEET_FOT_MATRIX,
    SHEET_POSITION_LIMITS,
    SHEET_SECRET_ALLOWANCES,
    SHEET_SETTINGS,
)
from fot_planner.excel.workbook_format import format_workbook
from fot_planner.defaults.settings import (
    DEFAULT_GOZ_AVERAGE_SALARY_LIMIT,
    DEFAULT_SETTINGS_ROW,
)
from fot_planner.position_reference import (
    default_position_reference,
    normalize_position,
)
from fot_planner.salary_limits_2556 import default_position_salary_limits


LAUNCH_SHEET_NAME = "Запуск"


_LAUNCH_MACRO = r'''
Option Explicit

Private Function FindFotPlannerExe(ByVal startDir As String) As String
    Dim fso As Object
    Dim folder As String
    Dim candidate As String
    Dim i As Integer

    Set fso = CreateObject("Scripting.FileSystemObject")
    folder = startDir

    For i = 1 To 8
        candidate = fso.BuildPath(folder, ".venv\Scripts\fot-planner.exe")
        If fso.FileExists(candidate) Then
            FindFotPlannerExe = candidate
            Exit Function
        End If
        If Not fso.FolderExists(folder) Then Exit For
        If fso.GetFolder(folder).IsRootFolder Then Exit For
        folder = fso.GetParentFolderName(folder)
    Next i

    FindFotPlannerExe = "fot-planner"
End Function

Public Sub RunFotPlanner()
    Dim inputPath As String
    Dim outputPath As String
    Dim baseName As String
    Dim dotPos As Integer
    Dim exePath As String
    Dim command As String

    If Len(ThisWorkbook.Path) = 0 Then
        MsgBox "Сначала сохраните файл.", vbExclamation, "FOT Planner"
        Exit Sub
    End If

    ThisWorkbook.Save

    inputPath = ThisWorkbook.FullName
    dotPos = InStrRev(ThisWorkbook.Name, ".")
    If dotPos > 1 Then
        baseName = Left(ThisWorkbook.Name, dotPos - 1)
    Else
        baseName = ThisWorkbook.Name
    End If
    outputPath = ThisWorkbook.Path & Application.PathSeparator & baseName & "_result.xlsx"
    exePath = FindFotPlannerExe(ThisWorkbook.Path)

    command = "cmd.exe /k " & Chr(34) & Chr(34) & exePath & Chr(34) & _
        " solve -i " & Chr(34) & inputPath & Chr(34) & _
        " -o " & Chr(34) & outputPath & Chr(34) & _
        " --time-limit 120" & Chr(34)

    CreateObject("WScript.Shell").Run command, 1, False
End Sub
'''.strip()


def _default_position_limits_table() -> pd.DataFrame:
    reference_by_position = {
        normalize_position(row.position): row
        for row in default_position_reference()
    }
    limits_by_position = {
        normalize_position(row.position): row
        for row in default_position_salary_limits()
    }
    rows: list[dict[str, object]] = []
    for key in sorted(reference_by_position | limits_by_position):
        reference = reference_by_position.get(key)
        limits = limits_by_position.get(key)
        position = (
            limits.position
            if limits is not None
            else reference.position
            if reference is not None
            else key
        )
        rows.append(
            {
                "должность": position,
                "категория персонала": limits.personnel_category if limits else "",
                "страница": reference.salary_page if reference else "",
                "номер группы": reference.salary_group_number if reference else "",
                "номер уровня": reference.level if reference else "",
                "оклад": (
                    reference.reference_salary_for_rate
                    if reference and reference.reference_salary_for_rate is not None
                    else ""
                ),
                "П2556": (
                    limits.order_2556_limit
                    if limits and limits.order_2556_limit is not None
                    else ""
                ),
                "П4": (
                    limits.p4_limit
                    if limits and limits.p4_limit is not None
                    else ""
                ),
                "БЭП": DEFAULT_GOZ_AVERAGE_SALARY_LIMIT,
                "примечание": limits.note if limits and limits.note else "",
            }
        )
    return pd.DataFrame(rows)


def _add_excel_launch_macro(source_path: Path, output_path: Path) -> None:
    if output_path.suffix.lower() != ".xlsm":
        return
    if os.name != "nt":
        raise RuntimeError("Macro-enabled templates with a launch button can only be built on Windows with Excel.")

    macro_b64 = base64.b64encode(_LAUNCH_MACRO.encode("utf-8")).decode("ascii")
    script = r'''
param(
    [Parameter(Mandatory=$true)][string]$SourceWorkbookPath,
    [Parameter(Mandatory=$true)][string]$OutputWorkbookPath,
    [Parameter(Mandatory=$true)][string]$MacroTextB64
)

$ErrorActionPreference = "Stop"
$sourcePath = (Resolve-Path -LiteralPath $SourceWorkbookPath).Path
$outputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputWorkbookPath)
$macroText = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($MacroTextB64))

$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false

try {
    $workbook = $excel.Workbooks.Open($sourcePath)

    foreach ($sheet in @($workbook.Worksheets)) {
        if ($sheet.Name -eq "Запуск") {
            $sheet.Delete()
            break
        }
    }

    $worksheet = $workbook.Worksheets.Add($workbook.Worksheets.Item(1))
    $worksheet.Name = "Запуск"
    $worksheet.Range("A1").Value2 = "Запуск оптимизатора"
    $worksheet.Range("A3").Value2 = "1. Заполните и сохраните входные листы."
    $worksheet.Range("A4").Value2 = "2. Нажмите кнопку ниже."
    $worksheet.Range("A5").Value2 = "3. Результат будет сохранен рядом с этой книгой как *_result.xlsx."
    $worksheet.Range("A1").Font.Bold = $true
    $worksheet.Range("A1").Font.Size = 18
    $worksheet.Columns.Item("A").ColumnWidth = 76

    $button = $worksheet.Buttons().Add(20, 105, 210, 42)
    $button.Caption = "Запустить расчет"
    $button.OnAction = "RunFotPlanner"
    $button.Font.Bold = $true

    $module = $workbook.VBProject.VBComponents.Add(1)
    $module.Name = "FotPlannerLauncher"
    $module.CodeModule.AddFromString($macroText)

    $worksheet.Activate()
    $workbook.SaveAs($outputPath, 52)
    $workbook.Close($false)
}
finally {
    if ($excel -ne $null) {
        $excel.Quit()
    }
}
'''.strip()

    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as tmp:
        tmp.write(script)
        script_path = tmp.name
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script_path,
                str(source_path),
                str(output_path),
                macro_b64,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            "Failed to add Excel launch macro. Enable Excel setting "
            "'Trust access to the VBA project object model' and try again. "
            f"Details: {message}"
        )


def create_template(path: str | Path) -> None:
    path = Path(path)
    write_path = path
    temp_xlsx_path: Path | None = None
    if path.suffix.lower() == ".xlsm":
        handle, temp_name = tempfile.mkstemp(
            suffix=".xlsx",
            prefix=f"{path.stem}_",
            dir=path.parent,
        )
        os.close(handle)
        temp_xlsx_path = Path(temp_name)
        write_path = temp_xlsx_path

    year = date.today().year

    employees = pd.DataFrame(
        [
            {
                "код строки": "E001-1",
                "фио": "Иванов Иван Иванович",
                "должность": "инженер",
                "подразделение": "лаборатория",
                "ставка": 1.0,
                "тип занятости": "основное",
                "категория занятости": "основной",
                "зарплата": 100000,
                "дата начала": f"{year}-01-01",
                "дата окончания": "",
                "разрешенные договоры": "",
                "запрещенные договоры": "",
            }
        ]
    )
    contract_labor = pd.DataFrame(
        columns=[
            "договор",
            "год",
            "трудоемкость",
            "должность",
            "страница",
            "номер группы",
            "номер уровня",
            "средняя стоимость выполнения работ в месяц",
        ]
    )
    contracts = pd.DataFrame(
        [
            {
                "код": "C001",
                "название": "НИОКР Альфа",
                "номер": "123/2025",
                "тип договора": "госзаказ",
                "счет": "2301",
                "ГОЗ": True,
                "дата начала": f"{year}-01-01",
                "дата окончания": f"{year}-12-31",
                "фот": 1_200_000,
                "оклад разрешен": True,
                "120 разрешена": False,
                "122 разрешена": True,
                "124 разрешена": False,
                "152 разрешена": False,
                "стимулирующая приказом разрешена": False,
                "проект Приоритет": "нет",
                "основное место разрешено": "да",
                "совместительство разрешено": "да",
                "конечная дата выплат оклада": f"{year}-12-31",
                "конечная дата выплат надбавок": f"{year}-12-31",
            }
        ]
    )
    secret_allowances = pd.DataFrame(
        columns=[
            "сотрудник",
            "договор секретности",
            "ставка 120",
        ]
    )
    settings = pd.DataFrame([{"год": year, **DEFAULT_SETTINGS_ROW}])
    fot_by_month = pd.DataFrame({"договор": ["C001"]})
    for m in range(1, 13):
        fot_by_month[str(m)] = [1_200_000 if m == 1 else ""]

    with pd.ExcelWriter(write_path, engine="openpyxl") as writer:
        employees.to_excel(writer, sheet_name=SHEET_EMPLOYEES, index=False)
        contracts.to_excel(writer, sheet_name=SHEET_CONTRACTS, index=False)
        secret_allowances.to_excel(writer, sheet_name=SHEET_SECRET_ALLOWANCES, index=False)
        contract_labor.to_excel(writer, sheet_name=SHEET_CONTRACT_LABOR, index=False)
        fot_by_month.to_excel(writer, sheet_name=SHEET_FOT_MATRIX, index=False)
        settings.to_excel(writer, sheet_name=SHEET_SETTINGS, index=False)
        _default_position_limits_table().to_excel(
            writer,
            sheet_name=SHEET_POSITION_LIMITS,
            index=False,
        )

    wb = load_workbook(write_path)
    yellow = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
    for sheet_name in (
        SHEET_CONTRACTS,
        SHEET_EMPLOYEES,
        SHEET_SECRET_ALLOWANCES,
    ):
        ws = wb[sheet_name]
        for cell in ws[1]:
            cell.fill = yellow
    wb.save(write_path)
    format_workbook(write_path)

    if temp_xlsx_path is not None:
        try:
            _add_excel_launch_macro(temp_xlsx_path, path)
        finally:
            temp_xlsx_path.unlink(missing_ok=True)
