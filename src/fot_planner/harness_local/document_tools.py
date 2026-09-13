"""Чистые операции известных форм. Не импортируют приложение и LLM."""
from pathlib import Path

STAFF_SHEETS = frozenset(('сотрудники', '120_надбавка'))
CONTRACT_SHEETS = frozenset(('договоры', 'фот_по_месяцам',
                            'трудоемкость_по_договорам', 'трудоёмкость_по_договорам'))


def template_kind(path):
    """Сохраняет прежний приоритет листов intake; неизвестная форма — None."""
    if Path(path).suffix.lower() not in ('.xlsx', '.xlsm'):
        return {'kind': None}
    import openpyxl
    try:
        workbook = openpyxl.load_workbook(path, read_only=True)
        try:
            names = {str(n).strip().lower() for n in workbook.sheetnames}
        finally:
            workbook.close()
    except Exception:  # Прежний контракт: непригодная книга передаётся следующему разбору.
        return {'kind': None}
    if names & CONTRACT_SHEETS:
        return {'kind': 'документ по договору'}
    if names & STAFF_SHEETS:
        return {'kind': 'штатное расписание'}
    return {'kind': None}
