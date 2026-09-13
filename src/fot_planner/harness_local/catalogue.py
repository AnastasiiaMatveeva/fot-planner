"""L2.1: локальные описания и выбор процедуры; без импорта app и сети.

Дескрипторы не дают права импортировать код. Привязки callable и Authority
передаёт установленный сервис, не модель/HTTP-клиент. Это ограниченный
синхронный read-only executor, не worker, квалификация или выпуск L4.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

Text = Annotated[str, StringConstraints(min_length=1)]
Key = Annotated[str, StringConstraints(pattern=r'^[a-z][a-z0-9._-]*$')]
Version = Annotated[str, StringConstraints(pattern=r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')]
TRUSTED_HANDLERS = frozenset({'document.template_kind'})


class CatalogueError(ValueError):
    pass


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class Ref(Contract):
    id: Key
    version: Version


class Descriptor(Contract):
    schema_version: Literal[1]
    kind: Literal['role', 'capability', 'skill', 'tool']
    id: Key
    version: Version
    owner: Text
    purpose: Text
    maturity: Literal['declared', 'implemented', 'qualified', 'suspended']
    domains: tuple[Text, ...] = Field(min_length=1)
    experimental: bool = False
    name: Text | None = None
    order: int | None = Field(default=None, ge=1)
    legacy: bool = False
    limitations: str = ''
    task: Key | None = None
    input_schema: Key | None = None
    output_schema: Key | None = None
    skills: tuple[Ref, ...] = ()
    tools: tuple[Ref, ...] = ()
    checks: tuple[Ref, ...] = ()
    handler: Key | None = None
    permissions: tuple[Text, ...] = ()
    resources: tuple[Text, ...] = ()
    effects: Literal['none', 'read-file'] = 'none'
    instruction: Text | None = None

    @model_validator(mode='after')
    def complete(self):
        if self.kind == 'role':
            if not self.name or self.order is None:
                raise ValueError('роли нужны имя и порядок')
            if self.legacy and not self.limitations:
                raise ValueError('legacy требует явных ограничений')
        else:
            if not self.input_schema or not self.output_schema:
                raise ValueError('нужны схемы входа и выхода')
            if self.kind == 'capability' and (not self.task or len(self.skills) != 1):
                raise ValueError('L2.1: capability требует task и ровно один skill')
            if self.kind == 'skill' and (len(self.tools) != 1 or not self.instruction):
                raise ValueError('L2.1: skill требует инструкцию и ровно один tool')
            if self.kind == 'tool' and self.maturity == 'implemented' and not self.handler:
                raise ValueError('нет обработчика реализованного инструмента')
        return self


class DocumentInput(Contract):
    path: Text


class DocumentKind(Contract):
    kind: Literal['документ по договору', 'штатное расписание'] | None


IO_SCHEMAS = {'document-input': DocumentInput, 'document-kind': DocumentKind}


def schemas():
    return {name: dict(model.model_json_schema(), **{'$schema': 'https://json-schema.org/draft/2020-12/schema'})
            for name, model in {'catalogue': Descriptor, **IO_SCHEMAS}.items()}


class Task(Contract):
    kind: Key
    domain: Text


class Authority(Contract):
    permissions: frozenset[str] = frozenset()
    resources: frozenset[str] = frozenset()
    files: tuple[Path, ...] = ()
    trial: bool = False


class Selection(Contract):
    status: str
    reason: str = ''
    action: str = ''
    capability: Ref | None = None
    skill: Ref | None = None
    tool: Ref | None = None


def _validate(model, data):
    try:
        return model.model_validate_json(json.dumps(data, ensure_ascii=False, allow_nan=False))
    except (ValidationError, ValueError, TypeError) as exc:
        raise CatalogueError(str(exc)) from exc


class Catalogue:
    def __init__(self, descriptors):
        self.descriptors = tuple(descriptors)
        self._by_ref = {(d.id, d.version): d for d in self.descriptors}
        if len(self._by_ref) != len(self.descriptors):
            raise CatalogueError('повтор ID/версии')
        for d in self.descriptors:
            if d.maturity == 'qualified':
                raise CatalogueError('квалификация требует проверяемого решения L4')
            if d.handler is not None and (d.kind != 'tool' or d.handler not in TRUSTED_HANDLERS):
                raise CatalogueError('неизвестный/неуместный handler: ' + d.handler)
            if d.kind != 'role' and (d.input_schema not in IO_SCHEMAS or d.output_schema not in IO_SCHEMAS):
                raise CatalogueError('схема входа/выхода не зарегистрирована')
            if d.handler == 'document.template_kind' and (
                    d.input_schema != 'document-input' or d.output_schema != 'document-kind'
                    or d.effects != 'read-file' or 'document.read' not in d.permissions
                    or not {'python', 'openpyxl'} <= set(d.resources)):
                raise CatalogueError('описание ослабляет контракт доверенного обработчика')
            for refs, kind in ((d.skills, 'skill'), (d.tools, 'tool')):
                for ref in refs:
                    target = self.resolve(ref.id, ref.version)
                    if target.kind != kind:
                        raise CatalogueError('неверный тип ссылки: ' + ref.id)
                    if d.kind != 'role' and (d.input_schema, d.output_schema) != (target.input_schema, target.output_schema):
                        raise CatalogueError('несовместимые схемы процедуры и инструмента')
            # CAP-004 не имитируется: пока нет executor обязательных checks,
            # описание с ними блокируется, а не объявляется неприменимым.
            if d.checks:
                raise CatalogueError('исполнение обязательных checks ещё не реализовано')

    @classmethod
    def from_documents(cls, documents):
        return cls([_validate(Descriptor, d) for d in documents])

    @classmethod
    def load(cls, root):
        root = Path(root)
        try:
            for name, schema in schemas().items():
                registered = json.loads((root / 'schemas/local-harness' / (name + '.schema.json')).read_text(encoding='utf-8'))
                if registered != schema:
                    raise CatalogueError('зарегистрированная схема разошлась с runtime: ' + name)
            paths = sorted((root / 'capabilities').glob('*/*.json'))
            if not paths:
                raise CatalogueError('каталог пуст')
            documents = []
            for path in paths:
                row = json.loads(path.read_text(encoding='utf-8'))
                if path.parent.name != row.get('id') or path.stem != row.get('version'):
                    raise CatalogueError('путь не совпадает с ID/версией: ' + str(path))
                documents.append(row)
            return cls.from_documents(documents)
        except (OSError, ValueError) as exc:
            raise CatalogueError(str(exc)) from exc

    def resolve(self, key, version):
        try:
            return self._by_ref[key, version]
        except KeyError as exc:
            raise CatalogueError('нет версии %s@%s' % (key, version)) from exc

    def descriptor_digest(self, key, version):
        """Хеш описания, НЕ полный hash кода/модели/зависимостей VER-001."""
        row = self.resolve(key, version).model_dump(mode='json')
        return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()

    def agent_view(self):
        roles = [d for d in self.descriptors if d.kind == 'role']
        if len({d.id for d in roles}) != len(roles):
            raise CatalogueError('для интерфейса нужна одна закреплённая версия роли')
        return {d.id: {'n': d.order, 'name': d.name, 'does': d.purpose,
                       'real': d.maturity == 'implemented'} for d in roles}

    def select(self, task: Task, authority: Authority):
        candidates = [d for d in self.descriptors if d.kind == 'capability' and d.task == task.kind]
        if not candidates:
            return Selection(status='missing_capability', reason='нет описанной способности', action='уточнить задачу или зарегистрировать потребность')
        candidates = [d for d in candidates if task.domain in d.domains]
        if not candidates:
            return Selection(status='scope_denied', reason='область не поддерживается', action='указать поддерживаемую область')
        if len(candidates) != 1:
            return Selection(status='ambiguous', reason='несколько версий/маршрутов', action='закрепить единственную версию в локальном каталоге')
        cap = candidates[0]
        skill = self.resolve(cap.skills[0].id, cap.skills[0].version)
        tool = self.resolve(skill.tools[0].id, skill.tools[0].version)
        for d in (cap, skill, tool):
            if task.domain not in d.domains:
                return Selection(status='scope_denied', reason=d.id, action='проверить область всех зависимостей')
            if d.experimental and not authority.trial:
                return Selection(status='trial_only', reason=d.id, action='использовать изолированную испытательную область')
            if d.maturity != 'implemented':
                return Selection(status='unavailable', reason=d.id + ': ' + d.maturity, action='восстановить или реализовать требуемую процедуру')
            if not set(d.permissions) <= authority.permissions:
                return Selection(status='permission_denied', reason=d.id, action='запросить необходимое локальное право')
            missing = set(d.resources) - authority.resources
            if missing:
                return Selection(status='unavailable', reason=', '.join(sorted(missing)), action='восстановить заявленный ресурс; новая способность не требуется')
        ref = lambda d: Ref(id=d.id, version=d.version)
        return Selection(status='ready', capability=ref(cap), skill=ref(skill), tool=ref(tool))

    def invoke(self, task, payload, authority, bindings):
        selection = self.select(task, authority)
        if selection.status != 'ready':
            raise CatalogueError(selection.status + ': ' + selection.reason + '; ' + selection.action)
        tool = self.resolve(selection.tool.id, selection.tool.version)
        arguments = _validate(IO_SCHEMAS[tool.input_schema], payload)
        if tool.effects == 'read-file':
            requested = Path(arguments.path).resolve()
            if requested not in {p.resolve() for p in authority.files}:
                raise CatalogueError('файл вне разрешённой области')
        handler = bindings.get(tool.handler)
        if not callable(handler):
            raise CatalogueError('обработчик недоступен: ' + str(tool.handler))
        output = handler(**arguments.model_dump())
        return _validate(IO_SCHEMAS[tool.output_schema], output).model_dump()


@lru_cache(maxsize=1)
def default_catalogue():
    """Закреплённый в процессе каталог; новая версия применяется после рестарта.

    Горячее переключение выпусков относится к L4. Здесь нельзя перечитать
    половину новых файлов между выбором процедуры и её исполнением.
    """
    return Catalogue.load(Path(__file__).resolve().parents[3])
