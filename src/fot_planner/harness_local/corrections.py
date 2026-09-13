"""MEM-001/002: точная область и обнаружение конфликтов, без доступа к app/БД.

Широкая применимость не выводится из имени документа или похожих слов.
LocalDecision хранит binding и автора; отсутствие binding = legacy_unknown.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .artifacts import sha256_of
from .decisions import digest


def source_of(document, *, period=None):
    try:
        current = sha256_of(document.path)
    except (OSError, TypeError):
        current = None
    if document.sha256 and current != document.sha256:
        current = None
    return {'document_id': document.id, 'sha256': current,
            'version': document.version, 'kind': document.kind,
            'scope': document.scope,
            'case_id': document.case_id if document.scope == 'план' else None,
            'period': period if document.scope == 'план' else None}


def content_of(correction):
    return {key: getattr(correction, key) for key in
            ('entity', 'wrong', 'right', 'note', 'document_kind', 'agent_version')}


def binding_for(source, confirmed):
    valid = bool(source['sha256'] and source['document_id'] and source['kind']
                 and source['scope'] in ('план', 'реестр')
                 and (source['scope'] != 'план' or (source['case_id'] and source['period'])))
    return {'schema_version': 1, 'purpose': 'extraction_correction', 'source': source,
            'status': ('confirmed' if confirmed else 'proposed') if valid else 'legacy_unknown',
            'recorded_at': datetime.now(timezone.utc).isoformat()}


def _normal(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _target(content):
    wrong = _normal(content['wrong'])
    if isinstance(wrong, dict):
        if 'поле' in wrong:
            # apply_edits сохраняет {идентификатор_строки: старое_значение, поле: ...}.
            wrong = {'field': wrong['поле'], 'keys': sorted(k for k in wrong if k != 'поле')}
        else:
            for key in ('code', 'employee_code', 'contract_code'):
                if wrong.get(key):
                    wrong = {key: wrong[key]}
                    break
    return digest(content['entity'], wrong)


def _eligible(records, source):
    """Только подтверждённые правки, непротиворечиво привязанные к этому источнику."""
    out = []
    for record in records:
        binding, content = record['binding'], record['content']
        if (not isinstance(binding, dict) or binding.get('schema_version') != 1
                or binding.get('purpose') != 'extraction_correction'
                or binding.get('status') != 'confirmed' or not source.get('sha256')
                or binding.get('source') != source or not record.get('actor')
                or record.get('actor_source') not in ('FOT_OPERATOR', 'учётная запись ОС')
                or record.get('subject_digest') != digest(binding, content)
                or not content.get('entity') or not content.get('wrong')):
            continue
        out.append(record)
    return out


def _groups(eligible):
    groups = {}
    for record in eligible:
        groups.setdefault(_target(record['content']), []).append(record)
    return groups


def conflict_groups(records, source, superseded=()):
    """Группы правок одного объекта/поля с разными значениями — списки ID.

    Правки, заменённые явным решением оператора, в спор не входят: конфликт
    считается разрешённым, пока не появится новое расходящееся значение.
    """
    superseded = set(superseded or ())
    eligible = [r for r in _eligible(records, source) if r['id'] not in superseded]
    out = []
    for group in _groups(eligible).values():
        answers = {digest(_normal(r['content']['right'])) for r in group}
        if len(answers) > 1:
            out.append(sorted(r['id'] for r in group))
    return sorted(out)


def resolution(keep_id, groups):
    """Что означает решение «оставить keep_id»: остальные ID его группы заменены.

    Правка вне конфликта разрешения не требует — это ошибка вызова, а не
    пустое решение: молчаливое «применено» скрыло бы, что решать было нечего.
    """
    for group in groups:
        if keep_id in group:
            return {'оставлено': keep_id, 'заменены': [i for i in group if i != keep_id]}
    raise ValueError('правка %s не участвует в конфликте' % keep_id)


def select(records, source, limit=8, superseded=()):
    """records: пары содержимого Correction и доказательства LocalDecision.

    Конфликты ищутся до limit. Все расходящиеся значения остаются в истории,
    но ни одно не побеждает только потому, что пришло позже; побеждает лишь
    явное решение оператора — заменённые правки исключаются, не удаляются.
    """
    superseded = set(superseded or ())
    eligible = [r for r in _eligible(records, source) if r['id'] not in superseded]
    conflicts = []
    allowed = []
    for group in _groups(eligible).values():
        answers = {digest(_normal(r['content']['right'])) for r in group}
        if len(answers) > 1:
            conflicts.extend(r['id'] for r in group)
        else:
            allowed.extend(group)
    hints, ids, seen = [], [], set()
    for row in sorted(allowed, key=lambda r: r['id'], reverse=True):
        c = row['content']
        same = digest(c['entity'], _normal(c['wrong']), _normal(c['right']))
        if same in seen or len(hints) >= max(0, limit):
            continue
        seen.add(same)
        text = '%s: %s — %s' % (c['entity'], c['wrong'][:160],
                                'верно ' + c['right'][:160] if c['right'] else 'неверно')
        if c['note']:
            text += ' (%s)' % c['note'][:120]
        hints.append(text)
        ids.append(row['id'])
    known = {r['id'] for r in records}
    return {'hints': hints, 'correction_ids': ids, 'conflicts': sorted(set(conflicts)),
            'superseded': sorted(i for i in superseded if i in known), 'source': source}
