"""Повторный числовой аудит закреплённого JSON-кандидата, без оптимизатора.

Пакет содержит исходный контекст, результат и требуемый набор. Его SHA
передаёт вызывающая сторона из доверенной записи, а не автор заключения.
Для закреплённого XLSX дополнительно сверяются значения основных таблиц;
полнота всех пользовательских представлений этим модулем не доказывается.
"""
from __future__ import annotations

import json
from io import BytesIO
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fot_planner.models import PlanningContext, PlanningResult
from fot_planner.harness_local.artifacts import sha256_bytes
from fot_planner.harness_local.manifest import canonical_json, code_version, default_src_root


class AuditBundle(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    schema_version: Literal[1] = Field(alias='schema')
    context: PlanningContext
    candidate: PlanningResult
    required_checks: list[str] = Field(min_length=1)
    input_sha256: str | None = None
    export_data_sha256: str | None = None

    @model_validator(mode='after')
    def coherent(self):
        if self.context.year != self.candidate.year:
            raise ValueError('год кандидата не совпадает с годом контекста')
        if len(set(self.required_checks)) != len(self.required_checks):
            raise ValueError('повтор обязательной проверки')
        return self


def bundle_bytes(context, candidate, required_checks, *, input_sha256=None, export_data_sha256=None):
    """Подготовить пакет для ArtifactStore; никаких записей на диск и вызовов модели."""
    bundle = AuditBundle.model_validate({'schema': 1, 'context': context,
                                         'candidate': candidate, 'required_checks': required_checks,
                                         'input_sha256': input_sha256, 'export_data_sha256': export_data_sha256})
    return canonical_json(bundle.model_dump(mode='json', by_alias=True))


def workbook_digest(raw: bytes):
    """Значения и формулы всех листов, кроме добавляемого сервисом «источники».

    Хеш байтов ZIP меняется и от оформления; здесь проверяются именно данные.
    Это контроль неизменности выгрузки, не доказательство корректности экспортёра.
    """
    from openpyxl import load_workbook
    book = load_workbook(BytesIO(raw), read_only=True, data_only=False)
    try:
        sheets = []
        for sheet in book:
            if sheet.title == 'источники':
                continue
            cells = []
            for row in sheet:
                for cell in row:
                    value = cell.value
                    if value is None:
                        continue
                    if isinstance(value, (date, datetime, time)):
                        value = value.isoformat()
                    cells.append([cell.coordinate, cell.data_type, value])
            sheets.append([sheet.title, cells])
        return sha256_bytes(canonical_json(sheets))
    finally:
        book.close()


def evaluate_bytes(raw: bytes, expected_sha256: str, *, input_sha256=None, workbook_bytes=None):
    if sha256_bytes(raw) != expected_sha256:
        raise ValueError('байты пакета не совпадают с доверенным SHA-256')

    def nonfinite(value):
        raise ValueError('недопустимое число: ' + value)

    json.loads(raw, parse_constant=nonfinite)
    bundle = AuditBundle.model_validate_json(raw, strict=True)
    bound = workbook_bytes is not None
    if bound:
        if not input_sha256 or bundle.input_sha256 != input_sha256:
            raise ValueError('пакет не соответствует закреплённому входу')
        if not bundle.export_data_sha256 or workbook_digest(workbook_bytes) != bundle.export_data_sha256:
            raise ValueError('данные конечного XLSX изменились после экспорта')
    from fot_planner.result_audit import audit
    # Не используем audit_status/checks/violations, записанные автором кандидата.
    report = audit(bundle.context, bundle.candidate, required=bundle.required_checks)
    active = any(c['status'] == 'passed' for c in report.checks)
    verdict = 'failed' if report.status == 'FAIL' else 'passed' if active else 'inconclusive'
    export_report = None
    if bound:
        from fot_planner.harness_local.export_audit import audit_export
        export_report = audit_export(workbook_bytes, bundle.context, bundle.candidate)
        if export_report['verdict'] != 'passed':
            verdict = 'failed'
    return {'schema': 1, 'bundle_sha256': expected_sha256,
            'scope': 'implemented-checks-on-bound-export' if bound else 'implemented-checks-on-json-candidate',
            'input_sha256': input_sha256 if bound else None,
            'output_sha256': sha256_bytes(workbook_bytes) if bound else None,
            'verdict': verdict,
            'acceptance': 'not_run', 'method': code_version(default_src_root()),
            'required_checks': bundle.required_checks, **report.as_dict(),
            'export_audit': export_report,
            'limitations': [('проверены данные основных таблиц экспорта; остальные представления и оформление не проверены'
                             if bound else 'связь JSON-кандидата с финальным XLSX не проверена'),
                            'измерения отражают выполненные сравнения шести действующих карточек, не все правила свода',
                            'пробелы независимого аудита из QA-001 сохраняются']}
