# -*- coding: utf-8 -*-
"""Реестр агентов и запись их работы.

Агенты в этом сервисе не автономны: каждый — короткая операция с началом,
концом и следом в базе. Экономист видит не «идет обработка», а кто именно
сейчас работает и сколько это заняло, потому что каждая операция открывает
строку в таблице activities и закрывает ее по завершении.

Честность реестра важнее полноты: у агента есть поле ``real``. Там, где за
экраном пока заготовленный пример, стоит False, и интерфейс говорит об этом
прямо, а не изображает работу.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager

from db import Activity, Message, now

# ключ -> (номер, короткое имя, что делает, работает ли по-настоящему)
AGENTS: dict[str, dict] = {
    "intake": {
        "n": 1, "name": "Извлечение данных",
        "does": "читает документы по договорам и переносит показатели в дело",
        "real": True,
    },
    "tuning": {
        "n": 2, "name": "Настройка расчета",
        "does": "переводит требование экономиста в веса целевой функции",
        "real": True,
    },
    "infeasible": {
        "n": 3, "name": "Анализ невыполнимости",
        "does": "объясняет, почему решения нет: какое ограничение не сходится "
                "и что в реестре этому мешает",
        "real": True,
    },
    "scenario": {
        "n": 4, "name": "Сценарное моделирование",
        "does": "считает «что будет, если» тем же решателем и сравнивает с планом",
        "real": True,
    },
    "reconcile": {
        "n": 5, "name": "Сверка плана и факта",
        "does": "принимает фактические начисления и пересчитывает остаток года",
        "real": False,
    },
    "memo": {
        "n": 6, "name": "Пояснительные материалы",
        "does": "готовит текст записки по числам расчета",
        "real": True,
    },
    "norms": {
        "n": 7, "name": "Нормативная база",
        "does": "сверяет справочник с новой редакцией приказа",
        "real": True,
    },
}


#: Оптимизатор агентом не является: денежные величины считает модель
#: математического программирования, а не языковая. На схеме устройства сервиса
#: это оговорено отдельно, и панель работ не должна это смазывать.
SOLVER = {"key": "solver", "name": "Оптимизатор",
          "does": "ищет план: MIP, решатель HiGHS"}


# ── версии агентов ──────────────────────────────────────────────
#
# Агент здесь — не модель, а ее подсказка и схема ответа: поменялась
# подсказка — поменялось поведение, и это другая версия. Версия — отпечаток
# текстов, из которых агент собран. Ее пишем в каждую работу: тогда по журналу
# видно, какая версия что прочитала, а стенд может сказать «версия a1b2c3
# прошла 30 проверок из 30, версия d4e5f6 — 26». Без этого сравнивать
# версии нечем, и «агент стал лучше» остается ощущением.

def _prompt_texts(agent):
    """Тексты, из которых собран агент. Пусто — у агента нет модели."""
    import chat
    import llm

    if agent == "intake":
        return [llm.CLASSIFY_SYSTEM, llm.CLASSIFY_SHAPE, json.dumps(llm.CLASSIFY_SCHEMA),
                llm.FREEFORM_SYSTEM, llm.FREEFORM_SHAPE, json.dumps(llm.FREEFORM_SCHEMA),
                chat.SYSTEM, chat.SHAPE, json.dumps(chat.SCHEMA)]
    if agent == "norms":
        return [llm.SYSTEM, llm.JSON_SHAPE, json.dumps(llm.RESPONSE_SCHEMA)]
    return []


_VERSIONS: dict = {}


def version_of(agent):
    """Короткий отпечаток версии агента; «—» для агентов без модели."""
    if agent in _VERSIONS:
        return _VERSIONS[agent]
    import hashlib

    texts = _prompt_texts(agent)
    if not texts:
        _VERSIONS[agent] = "—"
    else:
        digest = hashlib.sha1("\n".join(texts).encode("utf-8")).hexdigest()
        _VERSIONS[agent] = digest[:7]
    return _VERSIONS[agent]


def agent_list():
    return [dict(key=k, **v) for k, v in sorted(AGENTS.items(), key=lambda kv: kv[1]["n"])]


def say(db, case_id, text, who="агент", agent=None, payload=None, to_agent=None):
    """Реплика в ленту дела."""
    m = Message(case_id=case_id, who=who, agent=agent, to_agent=to_agent, text=text,
                payload=json.dumps(payload, ensure_ascii=False) if payload else None)
    db.add(m)
    db.commit()
    return m


def handoff(db, case_id, sender, receiver, text, payload=None):
    """Передача работы от агента к агенту.

    Передачи в сервисе были и раньше — определив вид документа, агент ввода
    отдает нормативный документ агенту нормативной базы, — но происходили
    молча. Экономист видел результат и не видел, кто кому что передал.
    """
    return say(db, case_id, text, who="передача", agent=sender,
               to_agent=receiver, payload=payload)


@contextmanager
def working(db, case_id, agent, title):
    """Открыть строку работы агента и закрыть ее по выходу.

    Строка появляется до начала работы, а не после: пока агент думает над
    документом, экономист уже видит, что тот занят и чем именно.
    """
    act = Activity(case_id=case_id, agent=agent, title=title, state="идет")
    db.add(act)
    db.commit()
    t0 = time.time()
    holder = {"detail": None, "artifact": None}
    try:
        yield holder
    except Exception as exc:  # noqa: BLE001 — след об ошибке важнее падения
        act.state = "ошибка"
        act.detail = str(exc)[:500]
        act.finished = now()
        act.seconds = round(time.time() - t0, 1)
        db.commit()
        raise
    act.state = "готово"
    act.detail = holder["detail"]
    version = version_of(agent)
    if holder.get("artifact") or version != "—":
        artifact = dict(holder.get("artifact") or {})
        if version != "—":
            artifact["версия агента"] = version
        act.artifact = json.dumps(artifact, ensure_ascii=False)
    act.finished = now()
    act.seconds = round(time.time() - t0, 1)
    db.commit()
