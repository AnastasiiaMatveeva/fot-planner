"""QA-002: запись сообщённых проверок, НЕ независимая приёмка кандидата.

Пока CLI передаёт статусы, а не все измерения, отсутствующие доказательства
нельзя достраивать из слова OPTIMAL или из пересказа языковой модели.
"""
from __future__ import annotations

from copy import deepcopy
from math import isfinite

STATES = {'passed', 'failed', 'error', 'inconclusive', 'not_run', 'not_applicable'}


def _valid_measurement(m):
    def number(value):
        return type(value) in (int, float) and isfinite(value)
    if not isinstance(m, dict):
        return False
    actual = m.get('actual')
    return (isinstance(m.get('metric'), str) and bool(m['metric'])
            and isinstance(m.get('unit'), str) and bool(m['unit'])
            and (number(actual) or (m['unit'] == 'boolean' and type(actual) is bool))
            and type(m.get('passed')) is bool and isinstance(m.get('coordinates'), dict)
            and all(type(m.get(k, True)) is bool for k in ('lower_inclusive', 'upper_inclusive'))
            and number(m.get('absolute_tolerance')) and m['absolute_tolerance'] >= 0
            and all(k in m and (m[k] is None or number(m[k])) for k in ('lower', 'upper'))
            and (m.get('relative_tolerance') is None or
                 (number(m['relative_tolerance']) and m['relative_tolerance'] >= 0)))


def record_checks(manifest, *, output_sha256, solver_status, audit_status, checks):
    plan = manifest.get('evaluation_plan') or {}
    required = plan.get('required_checks') or []
    observed, problems = {}, []
    expected = {c['requirement'] for c in required}
    for row in checks or []:
        if not isinstance(row, dict) or not isinstance(row.get('rule_id'), str):
            problems.append('некорректная запись проверки')
            continue
        rule = row['rule_id']
        if rule in observed:
            problems.append('повтор проверки: ' + rule)
        if rule not in expected:
            problems.append('проверка вне закреплённого набора: ' + rule)
        observed[rule] = row
    records = []
    for descriptor in required:
        rule = descriptor['requirement']
        row = observed.get(rule, {})
        status = row.get('status', 'not_run')
        reason = row.get('reason', '')
        if not isinstance(reason, str):
            status, reason = 'error', 'некорректное основание'
        if not isinstance(status, str) or status not in STATES:
            status, reason = 'error', 'неизвестное состояние проверки'
        if status == 'not_applicable' and not reason.strip():
            status, reason = 'error', 'нет основания неприменимости'
        if not row:
            reason = 'решатель не сообщил обязательную проверку'
        findings = row.get('findings')
        if findings is not None:
            if not isinstance(findings, list) or not all(isinstance(f, dict) for f in findings):
                status, reason = 'error', 'некорректный список нарушений'
            elif status in {'passed', 'not_applicable'} and any(f.get('severity') == 'error' for f in findings):
                status, reason = 'error', 'положительное состояние противоречит найденному нарушению'
        measurements = row.get('measurements')
        measurement_gap = 'CLI передал состояние без полного набора измерений'
        if measurements is not None:
            if (type(row.get('measurement_schema')) is not int or row['measurement_schema'] != 1
                    or not isinstance(measurements, list)
                    or not all(_valid_measurement(m) for m in measurements)):
                status, reason = 'error', 'некорректная схема измерений'
            elif status in {'passed', 'not_applicable'} and any(not m['passed'] for m in measurements):
                status, reason = 'error', 'положительное состояние противоречит измерению'
            else:
                measurement_gap = None if measurements else 'нет объектов сравнения'
        records.append({**deepcopy(descriptor), 'rule_id': rule, 'status': status,
                        'findings': deepcopy(findings),
                        'reason': reason, 'measurements': deepcopy(measurements),
                        'measurement_schema': row.get('measurement_schema'),
                        'measurement_gap': measurement_gap})
    states = {r['status'] for r in records}
    if problems or 'error' in states:
        reported = 'error'
    elif audit_status == 'FAIL' or 'failed' in states:
        reported = 'failed'
    elif 'not_run' in states:
        reported = 'not_run'
    elif (not required or 'inconclusive' in states or 'passed' not in states
          or not output_sha256 or audit_status != 'OK'
          or solver_status not in {'OPTIMAL', 'FEASIBLE'}):
        reported = 'inconclusive'
    else:
        reported = 'passed'
    solver = manifest.get('solver') or {}
    return {
        'schema': 1, 'scope': 'solver-reported-checks',
        'subject': {'run_id': manifest['run_id'],
                    'input_sha256': manifest['input']['sha256'], 'output_sha256': output_sha256},
        'method': deepcopy({k: solver.get(k) for k in ('code_sha256', 'python', 'dependencies', 'command')}),
        'checks': records, 'observed_checks': deepcopy(checks or []), 'problems': problems,
        'reported_verdict': reported,
        # Положительный протокол не доказывает независимость и полноту проверки.
        'verdict': 'inconclusive' if reported == 'passed' else reported,
        'independent': False, 'local_acceptance': 'not_run',
        'actuality': 'not_checked', 'capability_maturity': 'not_assessed',
        'limitations': ['нет отдельного независимого исполнения проверок',
                        'нет полного набора измерений, единиц и допусков каждой проверки',
                        'аудит выполнялся до финальных преобразований отчёта; повторный аудит не выполнен'],
    }
