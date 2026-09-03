# -*- coding: utf-8 -*-
"""Разбор документа об обновлении сумм языковой моделью.

Зачем отдельный слой: якорный разбор в serve.py требует, чтобы в документе была
таблица с заголовками «должность», «оклад», «П2556». Реальные выписки из приказов
так не выглядят: заголовок бывает в два ряда, объединен, назван «Наименование
должности» или «Должностной оклад, руб.», часть величин вынесена в текст над
таблицей. Модель читает документ целиком и возвращает плоский список величин
с основанием и датой начала действия.

Модель ничего не записывает: результат идет в дифф, который экономист
подтверждает вручную. Без ключа модуль сообщает, что недоступен, и вызывающая
сторона откатывается на якорный разбор.

Провайдер выбирается настройками, а не кодом:
    FOT_LLM_BASE_URL   -> модель на сервере организации (vLLM, OpenAI-совместимый
                          API), разбор со схемой — сервер сам держит генерацию
                          в ее рамках; идет первым, документ не покидает сеть
    ANTHROPIC_API_KEY  -> Claude, разбор со схемой (structured outputs)
    DEEPSEEK_API_KEY   -> DeepSeek, разбор в режиме JSON
Схему во всех случаях проверяем еще и у себя: модель может вернуть что угодно.

Дополнительно для сервера организации:
    FOT_LLM_MODEL         имя модели, в коде не зашито
    FOT_LLM_API_KEY       ключ, если vLLM поднят с --api-key
    FOT_LLM_INSECURE_TLS  1 для https с самоподписанным сертификатом
    FOT_LLM_NO_SCHEMA     1, если сервер не умеет json_schema
    FOT_LLM_THINKING      1, чтобы вернуть режим рассуждения моделям Qwen3
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

import docread

FIELDS = ("оклад", "П2556", "П4")
MAX_CHARS = 60000          # хватает на выписку в несколько листов
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


def _truthy(value):
    return (value or "").strip().lower() in {"1", "true", "yes", "да", "on"}


def _ssl_context():
    """Связка корневых сертификатов для запроса.

    В хранилище Windows на рабочих машинах набор корней урезан: у api.deepseek.com
    сертификат Amazon, которого там нет, и проверка падает с
    CERTIFICATE_VERIFY_FAILED. Берем полную связку certifi, если она установлена,
    иначе остаемся на системной.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def provider():
    """(имя провайдера, модель, причина недоступности).

    Порядок выбора неслучаен: модель в своей сети идет первой. Документы по ГОЗ
    при разборе уходят провайдеру целиком, и для них единственный приемлемый
    вариант — сервер организации. Облачные подключаются, только если локальный
    адрес не задан, либо провайдер выбран явно через FOT_LLM_PROVIDER.
    """
    forced = os.environ.get("FOT_LLM_PROVIDER", "").strip().lower()
    base_url = (os.environ.get("FOT_LLM_BASE_URL") or "").strip().rstrip("/")

    if forced in ("", "local", "server", "vllm") and base_url:
        model = os.environ.get("FOT_LLM_MODEL")
        if not model:
            return None, None, ("задан FOT_LLM_BASE_URL, но не задан FOT_LLM_MODEL — "
                                "имя модели в коде не зашито")
        return "local", model, ""

    if forced != "deepseek" and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return None, None, "задан ANTHROPIC_API_KEY, но не установлен пакет anthropic"
        return "anthropic", os.environ.get("FOT_LLM_MODEL", "claude-opus-5"), ""

    if os.environ.get("DEEPSEEK_API_KEY"):
        return "deepseek", os.environ.get("FOT_LLM_MODEL", "deepseek-chat"), ""

    if forced in ("local", "server", "vllm"):
        return None, None, "для provider=%s нужен FOT_LLM_BASE_URL" % forced
    return None, None, "не задан ни FOT_LLM_BASE_URL, ни ANTHROPIC_API_KEY, ни DEEPSEEK_API_KEY"


def available():
    name, _model, why = provider()
    return (name is not None), why


# ── документ в текст ────────────────────────────────────────────
# Нормативные документы приходят книгами Excel, PDF и старыми .doc — приведение
# к тексту вынесено в docread, здесь остается только запрос к модели.
def workbook_to_text(path):
    """Совместимость: раньше умели только книги Excel."""
    return docread.to_text(path, MAX_CHARS)


SYSTEM = """Ты разбираешь документ российской организации об оплате труда: выписку
из приказа, письмо военного представительства, положение об оплате труда или
приложение к ним. Нужно извлечь величины, относящиеся к должностям.

Извлекай только три вида величин:
- «оклад» — должностной оклад за одну полную ставку в месяц, рублей;
- «П2556» — предельный размер оклада по приказу № 2556, рублей;
- «П4» — предельный размер по приказу № 4, обычно задан категории персонала
  (НТП, НР), а не должности.

Правила:
1. Должность записывай так, как она названа в документе, без номера строки.
2. Значение — число в рублях. Если в документе тысячи рублей, приведи к рублям.
3. Не выдумывай величин, которых в документе нет: пустая ячейка, прочерк и
   «не установлен» означают отсутствие значения — такую строку пропусти.
4. Заголовки бывают в несколько строк и объединенными: соотнеси значение с
   правильной колонкой по координатам ячеек.
5. Дату начала действия и основание (номер и дата приказа или письма) возьми
   из шапки документа, если они там есть.

Если документ не об оплате труда должностей, верни пустой список rows и объясни
это в notes."""

JSON_SHAPE = """Ответь одним объектом JSON без пояснений вокруг:
{
  "document_kind": "что это за документ, одной фразой",
  "basis": "номер и дата приказа или письма, либо null",
  "effective_from": "ДД.ММ.ГГГГ, либо null",
  "rows": [{"position": "должность как в документе",
            "field": "оклад | П2556 | П4",
            "value": 12345,
            "cell": "лист и ячейка, откуда взято, либо null"}],
  "notes": "что осталось непонятным, либо null"
}"""


# ── проверка ответа ─────────────────────────────────────────────
def _clean(raw):
    """Отбросить строки, не подходящие под схему: модель могла ошибиться."""
    rows, dropped = [], 0
    for r in raw.get("rows") or []:
        if not isinstance(r, dict):
            dropped += 1
            continue
        pos = str(r.get("position") or "").strip()
        field = str(r.get("field") or "").strip()
        try:
            value = float(str(r.get("value")).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if not pos or field not in FIELDS or value <= 0:
            dropped += 1
            continue
        rows.append({"pos": pos, "field": field, "value": value,
                     "cell": r.get("cell") or None})
    return rows, dropped


#: Как провайдер называется в интерфейсе: экономисту важно, ушел документ
#: наружу или остался в сети организации.
PROVIDER_LABEL = {"local": "на сервере организации",
                  "anthropic": "Claude", "deepseek": "DeepSeek"}


def _normalize_date(value):
    """Дату модель может вернуть в ISO — приводим к принятому в документах виду."""
    if not value:
        return None
    v = str(value).strip()
    if len(v) == 10 and v[4] == "-" and v[7] == "-":
        return "%s.%s.%s" % (v[8:10], v[5:7], v[0:4])
    return v


def _result(raw, model):
    rows, dropped = _clean(raw)
    notes = raw.get("notes") or ""
    if dropped:
        notes = (notes + " " if notes else "") + \
                "Отброшено строк, не подошедших под схему: %d." % dropped
    return {"ok": True, "model": model,
            "kind": raw.get("document_kind") or "",
            "basis": raw.get("basis") or None,
            "effective_from": _normalize_date(raw.get("effective_from")),
            "notes": notes.strip() or None,
            "rows": rows}


# ── провайдеры ──────────────────────────────────────────────────
def _call_anthropic(text, filename, model):
    from typing import Literal, Optional

    import anthropic
    from pydantic import BaseModel, Field

    class Row(BaseModel):
        position: str = Field(description="должность или категория персонала, как в документе")
        field: Literal["оклад", "П2556", "П4"]
        value: float = Field(description="значение в рублях")
        cell: Optional[str] = Field(default=None, description="лист и ячейка, откуда взято")

    class Update(BaseModel):
        document_kind: str
        basis: Optional[str] = None
        effective_from: Optional[str] = None
        rows: list[Row]
        notes: Optional[str] = None

    client = anthropic.Anthropic()
    msg = client.messages.parse(
        model=model, max_tokens=8000, system=SYSTEM,
        messages=[{"role": "user", "content": "Документ «%s»:\n\n%s" % (filename, text)}],
        output_format=Update,
    )
    if msg.parsed_output is None:
        return None
    return msg.parsed_output.model_dump()


#: Схема ответа для серверов, умеющих держать генерацию в ее рамках.
#: Пишем руками, а не из pydantic: тот дает Optional через anyOf, а xgrammar
#: в vLLM надежнее работает с перечислением типов.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "document_kind": {"type": "string"},
        "basis": {"type": ["string", "null"]},
        "effective_from": {"type": ["string", "null"]},
        "rows": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "position": {"type": "string"},
                "field": {"type": "string", "enum": list(FIELDS)},
                "value": {"type": "number"},
                "cell": {"type": ["string", "null"]}},
            "required": ["position", "field", "value"],
            "additionalProperties": False}},
        "notes": {"type": ["string", "null"]}},
    "required": ["document_kind", "rows"],
    "additionalProperties": False,
}


class Truncated(Exception):
    """Ответ модели оборван по лимиту длины, но часть строк удалось спасти.

    Раньше это была обычная ошибка разбора JSON: кусок документа пропадал
    целиком, а в ленте оставалось «Unterminated string starting at: line 130».
    Спасенное лежит в ``data`` — вызывающая сторона решает, брать ли его.
    """

    def __init__(self, data, reason=""):
        super().__init__(reason or "ответ модели оборван по длине")
        self.data = data


def _salvage(raw):
    """Разобрать оборванный JSON, отрезав его по последнему целому значению.

    Модель пишет массивы объектов, и обрыв приходится на середину очередного
    объекта. Все, что закончилось до него, — годные строки, и терять их из-за
    последней недописанной незачем.
    """
    stack, in_string, escaped, stops = [], False, False, []
    for i, ch in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            stack.append(ch)
        elif ch in "]}":
            if stack:
                stack.pop()
            stops.append((i + 1, list(stack)))

    for pos, open_at in reversed(stops[-400:]):
        tail = "".join("]" if c == "[" else "}" for c in reversed(open_at))
        try:
            return json.loads(raw[:pos] + tail)
        except ValueError:
            continue
    return None


def _call_openai_compatible(text, filename, model, url, api_key=None,
                            schema=False, no_thinking=False, insecure=False,
                            system=None, shape=None, json_schema=None,
                            schema_name="reference_update", max_tokens=8000):
    """Запрос к любому серверу с OpenAI-совместимым API.

    Один адаптер на облачный DeepSeek и на vLLM в своей сети: протокол тот же,
    отличаются адрес и две настройки. ``schema`` включает управляемую генерацию
    по JSON-схеме — сервер сам не дает модели выйти за ее рамки; DeepSeek этого
    не обещает, поэтому там остается обычный режим JSON. ``no_thinking``
    выключает рассуждение у моделей Qwen3: с ним ответ из четырех токенов
    занимает сотню, а на длинном документе бюджет заканчивается раньше ответа.
    """
    system = system or SYSTEM
    shape = shape or JSON_SHAPE
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system if schema else system + "\n\n" + shape},
            {"role": "user", "content": "Документ «%s»:\n\n%s" % (filename, text)},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name,
                            "schema": json_schema or RESPONSE_SCHEMA,
                            "strict": True}}
    else:
        payload["response_format"] = {"type": "json_object"}
    if no_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    req = urllib.request.Request(
        url, method="POST", headers=headers,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    ctx = None
    if url.lower().startswith("https"):
        ctx = ssl._create_unverified_context() if insecure else _ssl_context()
    with urllib.request.urlopen(req, timeout=600, context=ctx) as resp:
        answer = json.loads(resp.read().decode("utf-8"))
    choice = answer["choices"][0]
    content = choice["message"]["content"]
    try:
        return json.loads(content)
    except ValueError as e:
        # Ответ не разобрался — почти всегда потому, что кончился бюджет
        # длины и объект оборвался на середине. Спасаем целые строки, а
        # решение, что с ними делать, оставляем вызывающей стороне.
        saved = _salvage(content)
        if saved is None:
            raise
        raise Truncated(saved, "%s: %s" % (choice.get("finish_reason") or "обрыв", e))


#: Сколько текста уходит в один запрос и насколько части перекрываются.
#: Перекрытие нужно, чтобы строка таблицы не разрезалась пополам на границе.
CHUNK = 40000
OVERLAP = 2000


def _chunks(text):
    """Длинный документ — по частям.

    Положение об оплате труда — 165 тысяч знаков и полторы сотни сумм; обрезать
    его до одного запроса значит не увидеть две трети документа. Режем по
    границам строк, чтобы таблицы не рвались посреди значения.
    """
    if len(text) <= CHUNK:
        return [text]
    parts, start = [], 0
    while start < len(text):
        end = min(start + CHUNK, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + CHUNK // 2, end)
            if cut > start:
                end = cut
        parts.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - OVERLAP, start + 1)
    return parts


def _ask(name, model, text, filename):
    """Один запрос к выбранному провайдеру."""
    if name == "anthropic":
        return _call_anthropic(text, filename, model)
    if name == "local":
        base = os.environ["FOT_LLM_BASE_URL"].strip().rstrip("/")
        return _call_openai_compatible(
            text, filename, model, base + "/chat/completions",
            api_key=os.environ.get("FOT_LLM_API_KEY"),
            schema=not _truthy(os.environ.get("FOT_LLM_NO_SCHEMA")),
            no_thinking=not _truthy(os.environ.get("FOT_LLM_THINKING")),
            insecure=_truthy(os.environ.get("FOT_LLM_INSECURE_TLS")))
    return _call_openai_compatible(text, filename, model, DEEPSEEK_URL,
                                   api_key=os.environ["DEEPSEEK_API_KEY"])


def parse(path, filename="", max_chunks=8):
    """Разобрать документ. Всегда возвращает dict; ok=False — откат на якорный разбор."""
    name, model, why = provider()
    if name is None:
        return {"ok": False, "unavailable": True, "error": why}

    try:
        text = docread.to_text(path, CHUNK * max_chunks)
    except docread.Unreadable as e:
        return {"ok": False, "error": str(e)}

    filename = filename or os.path.basename(path)
    parts = _chunks(text)
    merged, seen, notes = {"rows": []}, set(), []
    errors = 0
    for i, part in enumerate(parts, 1):
        label = filename if len(parts) == 1 else "%s, часть %d из %d" % (filename, i, len(parts))
        try:
            raw = _ask(name, model, part, label)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            errors += 1
            notes.append("часть %d: %s ответил %s %s" % (i, name, e.code, detail))
            continue
        except Exception as e:  # noqa: BLE001 — сеть, ключ, лимиты, битый JSON
            errors += 1
            notes.append("часть %d: %s" % (i, e))
            continue
        if not raw:
            continue
        for key in ("document_kind", "basis", "effective_from"):
            if not merged.get(key) and raw.get(key):
                merged[key] = raw[key]
        if raw.get("notes") and len(parts) == 1:
            notes.append(raw["notes"])
        for row in raw.get("rows") or []:
            k = (str(row.get("position", "")).strip().lower(), row.get("field"))
            if k in seen:                 # части перекрываются, повторы отбрасываем
                continue
            seen.add(k)
            merged["rows"].append(row)

    if errors == len(parts):
        return {"ok": False, "error": "; ".join(notes)[:400] or "модель не ответила"}
    if len(parts) > 1:
        notes.insert(0, "Документ разобран по частям: %d." % len(parts))
    merged["notes"] = " ".join(n for n in notes if n) or None
    return _result(merged, "%s, %s" % (model, PROVIDER_LABEL.get(name, name)))


# ── определение вида документа ──────────────────────────────────
#: Виды, каждый со своим обработчиком. «иное» — честный ответ, когда документ
#: не про оплату труда; агент тогда спрашивает экономиста, а не гадает.
DOC_KINDS = ("документ по договору", "нормативный документ",
             "правила замещения должностей", "штатное расписание", "иное")

CLASSIFY_SYSTEM = """Определи, что за документ перед тобой. Это документы
российской организации, относящиеся к планированию фонда оплаты труда.

Виды:
- «документ по договору» — расчетно-калькуляционные материалы, структура цены,
  калькуляция, сметы, формы с трудоемкостью и стоимостью работ по договору;
- «нормативный документ» — приказ, письмо, положение об оплате труда, справка
  об окладах и предельных размерах выплат;
- «правила замещения должностей» — таблица вида «должность → кем может быть
  замещена»;
- «штатное расписание» — перечень сотрудников с должностями и ставками;
- «иное» — все остальное.

Отвечай по существу документа, а не по названию файла.

Отдельно оцени, читаем ли сам текст. Бывает, что PDF собран из скана чужим
распознаванием, и вместо слов идет каша: латинские буквы посреди русских слов,
разорванные слова, бессмысленные сочетания знаков — «МИН ИСТRJ>Сrво»,
«(МИ НОSОР)». Тогда garbled = true. Опечатки, переносы, обрывки таблиц и
колонки цифр кашей не считаются: если документ в целом можно прочесть
глазами, garbled = false."""

CLASSIFY_SHAPE = """Ответь одним объектом JSON:
{"kind": "один из видов", "title": "как называется документ, одной фразой",
 "why": "по каким признакам решил, одной фразой",
 "garbled": false,
 "garbled_why": "если текст нечитаемый — чем именно, одной фразой, иначе null"}"""

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(DOC_KINDS)},
        "title": {"type": "string"},
        "why": {"type": "string"},
        "garbled": {"type": "boolean"},
        "garbled_why": {"type": ["string", "null"]},
    },
    "required": ["kind"],
    "additionalProperties": False,
}


def classify(path, filename="", head=6000):
    """Что за документ. Возвращает dict; ok=False — решать вызывающей стороне.

    Модели хватает начала документа: вид виден по шапке и первым строкам,
    а гонять через нее семьдесят шесть страниц положения об оплате труда
    ради одного слова незачем.
    """
    name, model, why = provider()
    if name is None:
        return {"ok": False, "unavailable": True, "error": why}
    try:
        text = docread.to_text(path, head)
    except docread.Unreadable as e:
        return {"ok": False, "error": str(e)}

    filename = filename or os.path.basename(path)
    try:
        if name == "anthropic":
            raw = _classify_anthropic(text, filename, model)
        else:
            raw = _call_openai_compatible(
                text, filename, model,
                _endpoint(name), api_key=_key(name),
                schema=_use_schema(name), no_thinking=_no_thinking(name),
                insecure=_truthy(os.environ.get("FOT_LLM_INSECURE_TLS")),
                system=CLASSIFY_SYSTEM, shape=CLASSIFY_SHAPE,
                json_schema=CLASSIFY_SCHEMA, schema_name="document_kind")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "%s ответил %s" % (name, e.code)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (name, e)}

    kind = (raw or {}).get("kind")
    if kind not in DOC_KINDS:
        return {"ok": False, "error": "модель вернула неизвестный вид: %r" % kind}
    return {"ok": True, "kind": kind, "title": (raw.get("title") or "").strip(),
            "why": (raw.get("why") or "").strip(),
            "garbled": bool(raw.get("garbled")),
            "garbled_why": (raw.get("garbled_why") or "").strip(),
            "model": "%s, %s" % (model, PROVIDER_LABEL.get(name, name))}


def _classify_anthropic(text, filename, model):
    from typing import Literal, Optional

    import anthropic
    from pydantic import BaseModel

    class Kind(BaseModel):
        kind: Literal["документ по договору", "нормативный документ",
                      "правила замещения должностей", "штатное расписание", "иное"]
        title: Optional[str] = None
        why: Optional[str] = None
        garbled: bool = False
        garbled_why: Optional[str] = None

    msg = anthropic.Anthropic().messages.parse(
        model=model, max_tokens=500, system=CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": "Документ «%s»:\n\n%s" % (filename, text)}],
        output_format=Kind)
    return msg.parsed_output.model_dump() if msg.parsed_output else None


def _endpoint(name):
    if name == "local":
        return os.environ["FOT_LLM_BASE_URL"].strip().rstrip("/") + "/chat/completions"
    return DEEPSEEK_URL


def _key(name):
    return os.environ.get("FOT_LLM_API_KEY") if name == "local" \
        else os.environ.get("DEEPSEEK_API_KEY")


def _use_schema(name):
    return name == "local" and not _truthy(os.environ.get("FOT_LLM_NO_SCHEMA"))


def _no_thinking(name):
    return name == "local" and not _truthy(os.environ.get("FOT_LLM_THINKING"))


# ── разбор документа неизвестной формы ───────────────────────────────────
#
# Экономисты просят загружать в сервис любой документ, а не четыре знакомые
# формы. Модель это вытянет, но записывать вытянутое прямо в реестр нельзя:
# если оклад взят не из той графы, расчет пройдет и даст правдоподобный
# неверный ответ, а это ГОЗ. Поэтому здесь только чтение — куда положить
# прочитанное, решает сервис, а подтверждает человек.
#
# У каждой строки спрашиваем, откуда она в документе. Без этого проверить
# предложение нельзя: экономисту пришлось бы искать значение в файле глазами.

FREEFORM_SYSTEM = """Ты читаешь документ планово-экономического отдела и
достаешь из него строки для расчета фонда оплаты труда.

Сотрудник (employees) — строка штатного расписания: табельный номер, ФИО,
должность, подразделение, доля ставки, месячная зарплата, срок работы. Тип
занятости — «основное» или «совместительство». Категория занятости —
«основной», «студент» или «аспирант». Разрешенные и запрещенные договоры,
если они названы. Один человек может быть задан несколькими строками с
разными должностями и ставками — это нормально, не объединяй их.

Договор (contracts): шифр, наименование, номер, вид работ, номер счета,
признак гособоронзаказа, срок действия, фонд оплаты труда. Разрешенные виды
выплат — оклад, 120, 122, 124, 152, стимулирующая приказом: по каждому «да»
или «нет», если в документе сказано. Разрешено ли основное место и
совместительство, приоритет, конечные даты выплат оклада и надбавок.

Трудоемкость (labor) — плановые позиции договора: шифр договора, год,
должность или окладная группа (страница, номер группы, номер уровня), план в
человеко-месяцах, средняя стоимость одного человеко-месяца в рублях. Это
главное содержание расчетно-калькуляционных материалов и Формы 9д.

Поступления (inflows) — деньги по договору помесячно: шифр договора, год,
номер месяца от 1 до 12, сумма поступления. Строку заводи на каждый месяц,
где сумма названа.

Надбавка 120 (secret): табельный номер сотрудника, шифр договора
секретности, доля ставки для 120.

Должность справочника (positions): название и категория персонала (НТП, НР,
АУП, ППС, ПП), а если в документе есть — оклад за полную ставку, предельный
размер по приказу № 2556, предельный размер по приказу № 4, БЭП. Это
перечень должностей организации, а не люди: ФИО у такой строки нет.

Правило замещения (substitutions): должность сотрудника и должности, которые
ему можно дать дополнительно к собственной.

Правила:
- бери только то, что в документе действительно написано; ничего не выводи
  по смыслу и не достраивай по образцу;
- у сотрудника обязательно заполни position и salary, если они в документе
  есть: должность и месячная зарплата — главное в строке штатного расписания,
  без них строка бесполезна;
- поля from и to — это только срок, даты вида 01.01.2026; если срок в
  документе не указан, оставь их пустыми и ничего туда не подставляй;
- для каждой строки заполни поле «место»: где она в документе — лист и номер
  строки, адрес ячейки или короткая цитата;
- суммы возвращай числом без пробелов и знака рубля; проценты — числом;
- перечень должностей с категориями — это positions, а не employees:
  сотрудника без ФИО и табельного номера не бывает;
- разрешения выплат отмечай словами «да» и «нет», а не галочками;
- если строк какого-то вида в документе нет, верни пустой список."""

FREEFORM_SHAPE = """Ответь одним объектом JSON:
{"employees": [{"code": "табельный", "fio": "ФИО", "position": "должность",
                "salary": 50000, "department": "подразделение", "rate": 1.0,
                "employment": "основное", "category": "основной",
                "allowed": "шифры через запятую", "forbidden": "шифры",
                "from": "01.01.2026", "to": "31.12.2026",
                "место": "лист «штат», строка 7"}],
 "contracts": [{"code": "шифр", "name": "наименование", "num": "номер",
                "type": "вид", "account": "счет", "goz": "да", "fot": 1000000,
                "allow_salary": "да", "allow_120": "нет", "allow_122": "да",
                "allow_124": "нет", "allow_152": "нет", "allow_order": "нет",
                "priority": null, "allow_main": "да", "allow_part": "да",
                "salary_deadline": null, "allowance_deadline": null,
                "from": "01.01.2026", "to": "31.12.2026",
                "место": "лист «договоры», строка 3"}],
 "labor": [{"contract": "шифр", "year": 2026, "position": "Инженер",
            "page": null, "group": null, "level": null,
            "person_months": 12.5, "avg_cost": 95000,
            "место": "Форма 9д, строка 14"}],
 "inflows": [{"contract": "шифр", "year": 2026, "month": 3, "amount": 1200000,
              "место": "график поступлений, строка 5"}],
 "secret": [{"employee": "табельный", "contract": "шифр договора секретности",
             "rate": 0.5, "место": "приказ, пункт 2"}],
 "substitutions": [{"position": "должность", "replaced_by": "кем",
                    "место": "строка 12"}],
 "positions": [{"position": "Инженер", "category": "НТП",
                "salary_for_rate": 40400, "p2556": 110000, "p4": 149648.9,
                "bep": 112261, "место": "лист «ШР», строка 20"}]}"""

_TXT = {"type": ["string", "null"]}
_NUM = {"type": ["number", "null"]}

_FF_EMP = {
    "type": "object",
    "properties": {
        "code": _TXT, "fio": _TXT, "position": _TXT, "salary": _NUM,
        "department": _TXT, "rate": _NUM, "employment": _TXT, "category": _TXT,
        "allowed": _TXT, "forbidden": _TXT,
        "from": _TXT, "to": _TXT, "место": _TXT,
    },
    # Должность, ставка и зарплата — обязательные: без них строка штатного
    # расписания бесполезна, а необязательное поле модель молча пропускает,
    # даже когда значение стоит прямо перед ней. Пустым его вернуть можно —
    # тип допускает null, — но соврать умолчанием уже не выйдет.
    "required": ["code", "position", "rate", "salary"],
    "additionalProperties": False,
}

_FF_LABOR = {
    "type": "object",
    "properties": {
        "contract": _TXT, "year": _NUM, "position": _TXT, "page": _TXT,
        "group": _NUM, "level": _NUM, "person_months": _NUM, "avg_cost": _NUM,
        "место": _TXT,
    },
    "required": ["contract", "person_months", "avg_cost"],
    "additionalProperties": False,
}

_FF_INFLOW = {
    "type": "object",
    "properties": {
        "contract": _TXT, "year": _NUM, "month": _NUM, "amount": _NUM,
        "место": _TXT,
    },
    "required": ["contract", "month", "amount"],
    "additionalProperties": False,
}

_FF_SECRET = {
    "type": "object",
    "properties": {
        "employee": _TXT, "contract": _TXT, "rate": _NUM, "место": _TXT,
    },
    "required": ["employee", "contract"],
    "additionalProperties": False,
}
_FF_CTR = {
    "type": "object",
    "properties": {
        "code": _TXT, "name": _TXT, "num": _TXT, "type": _TXT,
        "account": _TXT, "goz": _TXT, "fot": _NUM,
        "allow_salary": _TXT, "allow_120": _TXT, "allow_122": _TXT,
        "allow_124": _TXT, "allow_152": _TXT, "allow_order": _TXT,
        "priority": _TXT, "allow_main": _TXT, "allow_part": _TXT,
        "salary_deadline": _TXT, "allowance_deadline": _TXT,
        "from": _TXT, "to": _TXT, "место": _TXT,
    },
    "required": ["code", "fot", "goz"],
    "additionalProperties": False,
}
_FF_SUB = {
    "type": "object",
    "properties": {"position": _TXT, "replaced_by": _TXT, "место": _TXT},
    "required": ["position", "replaced_by"],
    "additionalProperties": False,
}
_FF_POS = {
    "type": "object",
    "properties": {
        "position": _TXT, "category": _TXT, "salary_for_rate": _NUM,
        "p2556": _NUM, "p4": _NUM, "bep": _NUM, "место": _TXT,
    },
    "required": ["position", "category"],
    "additionalProperties": False,
}
FREEFORM_SCHEMA = {
    "type": "object",
    "properties": {
        "employees": {"type": "array", "items": _FF_EMP},
        "contracts": {"type": "array", "items": _FF_CTR},
        "labor": {"type": "array", "items": _FF_LABOR},
        "inflows": {"type": "array", "items": _FF_INFLOW},
        "secret": {"type": "array", "items": _FF_SECRET},
        "substitutions": {"type": "array", "items": _FF_SUB},
        "positions": {"type": "array", "items": _FF_POS},
    },
    "required": ["employees", "contracts", "labor", "inflows", "secret",
                 "substitutions", "positions"],
    "additionalProperties": False,
}

#: По каким полям строка считается той же самой на стыке частей документа.
_FF_KEY = {"employees": ("code", "fio"), "contracts": ("code", "num"),
           "substitutions": ("position", "replaced_by"),
           "positions": ("position",),
           "labor": ("contract", "position", "person_months"),
           "inflows": ("contract", "month"),
           "secret": ("employee", "contract")}


def freeform(path, filename="", max_chunks=8, hints=None):
    """Достать сущности из документа любой формы. ok=False — не получилось.

    ``hints`` — правки экономиста по документам этого вида, строками. Они
    подмешиваются в подсказку: агент учится не переобучением модели, а тем,
    что видит свои прошлые ошибки перед новым разбором.
    """
    name, model, why = provider()
    if name is None:
        return {"ok": False, "unavailable": True, "error": why}

    try:
        text = docread.to_text(path, CHUNK * max_chunks)
    except docread.Unreadable as e:
        return {"ok": False, "error": str(e)}

    filename = filename or os.path.basename(path)
    found = {key: [] for key in _FF_KEY}
    seen = {k: set() for k in found}
    parts = _chunks(text)[:max_chunks]
    errors = []

    cut = 0
    queue = list(parts)
    while queue:
        part = queue.pop(0)
        try:
            raw = _ask_freeform(name, model, part, filename, hints)
        except Truncated as e:
            # Часть оказалась слишком плотной: ответ не поместился в бюджет.
            # Спасенное берем, а саму часть переспрашиваем половинами — так
            # находится и то, что не поместилось. Делим, пока есть что делить.
            cut += 1
            raw = e.data
            if len(part) > 4000:
                half = len(part) // 2
                edge = part.rfind("\n", half // 2, half + half // 2)
                if edge <= 0:
                    edge = half
                queue[:0] = [part[:edge], part[edge:]]
        except Exception as e:  # noqa: BLE001 — сеть, лимиты, битый JSON
            errors.append(str(e))
            continue
        if not isinstance(raw, dict):
            continue
        for key in found:
            for item in raw.get(key) or []:
                if not isinstance(item, dict):
                    continue
                # Части перекрываются на 2000 знаков, и строка со стыка
                # приходит дважды.
                mark = tuple(str(item.get(f) or "").strip().lower()
                             for f in _FF_KEY[key])
                if not any(mark) or mark in seen[key]:
                    continue
                seen[key].add(mark)
                found[key].append(item)

    if errors and not any(found.values()):
        return {"ok": False, "error": errors[0]}
    return {"ok": True, "model": "%s %s" % (name, model), "parts": len(parts),
            "обрезано частей": cut, **found}


def _hinted(system, hints):
    if not hints:
        return system
    return (system + "\n\nЭкономист уже поправлял разбор документов такого вида. "
            "Учитывай эти правки, они важнее общих правил:\n" +
            "\n".join("- " + h for h in hints[:8]))


def _ask_freeform(name, model, text, filename, hints=None):
    if name == "anthropic":
        return _freeform_anthropic(text, filename, model, hints)
    url = (os.environ["FOT_LLM_BASE_URL"].strip().rstrip("/") + "/chat/completions"
           if name == "local" else DEEPSEEK_URL)
    return _call_openai_compatible(
        text, filename, model, url, api_key=_key(name), schema=_use_schema(name),
        no_thinking=_no_thinking(name),
        insecure=_truthy(os.environ.get("FOT_LLM_INSECURE_TLS")),
        system=_hinted(FREEFORM_SYSTEM, hints), shape=FREEFORM_SHAPE,
        json_schema=FREEFORM_SCHEMA, schema_name="freeform_entities",
        # Семь сущностей и шесть десятков полей: на плотной части документа
        # ответ длиннее прежних восьми тысяч токенов, и его обрывало.
        max_tokens=16000)


def _freeform_anthropic(text, filename, model, hints=None):
    import anthropic

    msg = anthropic.Anthropic().messages.create(
        model=model, max_tokens=8000,
        system=_hinted(FREEFORM_SYSTEM, hints) + "\n\n" + FREEFORM_SHAPE,
        messages=[{"role": "user",
                   "content": "Документ «%s»:\n\n%s" % (filename, text)}])
    return _clean("".join(b.text for b in msg.content if b.type == "text"))
