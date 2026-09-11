# -*- coding: utf-8 -*-
"""Хранилище сервиса: SQLite через SQLAlchemy.

Что здесь лежит и почему именно в базе, а не в файлах:

* **дело** — рабочая папка экономиста на год. К нему привязано все остальное,
  и по нему экономист возвращается к работе через день и видит, где остановился;
* **документ** — что загрузили, когда, чем распознали, каким файлом лежит.
  Сам xlsx остается на диске, в базе путь и размер;
* **работа агента** — кто, когда, сколько длился, чем кончился. Это и есть
  «видно, как работают агенты»: интерфейс просто читает эту таблицу;
* **вопрос** — то, чего агент не понял. Пока без ответа, работа не идет дальше;
* **сообщение** — лента чата, чтобы вернувшийся экономист прочитал ход дела;
* **прогон** — расчет: настройки, статус решателя, время, результат. Для ГОЗ
  нужно уметь показать, на чем именно посчитан план.

Справочники и нормативы сюда не переносятся: они не накапливаются, им нужна
история правок с обоснованием, и это дешевле дает git.
"""
from __future__ import annotations

import datetime as dt
import os

from sqlalchemy import (
    DateTime, Float, ForeignKey, Integer, String, Text, create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("FOT_DATA_DIR") or os.path.join(HERE, "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
RESULT_DIR = os.path.join(DATA_DIR, "results")
# Неизменяемые артефакты по SHA-256: вход и результат прогона, снимки.
ARTIFACT_DIR = os.path.join(DATA_DIR, "artifacts")
DB_PATH = os.path.join(DATA_DIR, "fot.sqlite3")

for d in (DATA_DIR, UPLOAD_DIR, RESULT_DIR):
    os.makedirs(d, exist_ok=True)


def now():
    return dt.datetime.now()


class Base(DeclarativeBase):
    pass


class Case(Base):
    """Дело — планирование ФОТ на год. Точка возврата для экономиста."""

    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    year: Mapped[int] = mapped_column(Integer, default=2026)
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated: Mapped[dt.datetime] = mapped_column(DateTime, default=now, onupdate=now)
    # черновик | сбор данных | готово к расчету | посчитано
    stage: Mapped[str] = mapped_column(String(40), default="сбор данных")
    # Собранные из документов данные договоров, JSON. Лежат в деле, а не в
    # памяти процесса: экономист уходит и возвращается через день.
    passport: Mapped[str | None] = mapped_column(Text, default=None)
    # Документы, которые чат этого плана не смотрит: снятые флажки, JSON-список id.
    muted_docs: Mapped[str | None] = mapped_column(Text, default=None)
    # Переменные проектирования, которые экономист задал сам: закрепления
    # (ручные назначения), запреты и настройки расчёта. JSON, живёт с планом
    # и уходит в каждый расчёт, пока экономист не снимет.
    plan_settings: Mapped[str | None] = mapped_column(Text, default=None)

    employees: Mapped[list["Employee"]] = relationship(back_populates="case",
                                                       cascade="all, delete-orphan")
    contracts: Mapped[list["Contract"]] = relationship(back_populates="case",
                                                       cascade="all, delete-orphan")
    substitutions: Mapped[list["Substitution"]] = relationship(
        back_populates="case", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="case",
                                                       cascade="all, delete-orphan")
    messages: Mapped[list["Message"]] = relationship(back_populates="case",
                                                     cascade="all, delete-orphan")
    activities: Mapped[list["Activity"]] = relationship(back_populates="case",
                                                        cascade="all, delete-orphan")
    questions: Mapped[list["Question"]] = relationship(back_populates="case",
                                                       cascade="all, delete-orphan")
    runs: Mapped[list["Run"]] = relationship(back_populates="case",
                                             cascade="all, delete-orphan")


class Employee(Base):
    """Строка штатного расписания организации.

    Штатка не принадлежит плану: она одна на организацию и меняется приказами
    о приеме, переводе и увольнении, а не с каждым новым планом. Поэтому
    ``case_id`` пуст — строка общая.
    """

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), default=None)
    code: Mapped[str] = mapped_column(String(60))
    person_code: Mapped[str | None] = mapped_column(String(60), default=None)
    fio: Mapped[str | None] = mapped_column(String(200), default=None)
    position: Mapped[str | None] = mapped_column(String(200), default=None)
    rate: Mapped[float | None] = mapped_column(Float, default=None)
    salary: Mapped[float | None] = mapped_column(Float, default=None)
    date_from: Mapped[str | None] = mapped_column(String(20), default=None)
    date_to: Mapped[str | None] = mapped_column(String(20), default=None)
    # Поля входного листа «сотрудники», которых раньше не было. Без них не
    # выразить совместительство: тип занятости прописывался в файл расчета
    # жестко как «основное», а категория — как «основной», хотя у студентов и
    # аспирантов свои пределы суммарной ставки.
    department: Mapped[str | None] = mapped_column(String(200), default=None)
    employment_type: Mapped[str | None] = mapped_column(String(40), default=None)
    employment_category: Mapped[str | None] = mapped_column(String(40), default=None)
    allowed_contracts: Mapped[str | None] = mapped_column(String(300), default=None)
    forbidden_contracts: Mapped[str | None] = mapped_column(String(300), default=None)
    source: Mapped[str | None] = mapped_column(String(300), default=None)
    # Из какого документа взята строка. По имени файла связь ненадежна:
    # документ можно загрузить повторно или удалить, и данные должны уйти
    # вместе с ним, а не остаться сиротами.
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), default=None)

    case: Mapped[Case] = relationship(back_populates="employees")


class Contract(Base):
    """Договор: фонд, признак ГОЗ, разрешенные виды выплат.

    Договор длиннее плана: он заключается на несколько лет, а планов по нему
    столько же, сколько лет. Держать его условия в деле значит загружать два
    десятка документов каждый январь заново. Поэтому договоры — реестр
    организации, а план на год берет из него действующие в этом году.
    """

    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), default=None)
    # Срок действия: по нему план на год отбирает свои договоры.
    date_from: Mapped[str | None] = mapped_column(String(20), default=None)
    date_to: Mapped[str | None] = mapped_column(String(20), default=None)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str | None] = mapped_column(String(300), default=None)
    number: Mapped[str | None] = mapped_column(String(120), default=None)
    kind: Mapped[str | None] = mapped_column(String(60), default=None)
    goz: Mapped[str | None] = mapped_column(String(10), default=None)
    fund: Mapped[float | None] = mapped_column(Float, default=None)
    kinds: Mapped[str | None] = mapped_column(String(200), default=None)
    # Поля входного листа «договоры», которых не было. Счет важен по существу:
    # договор со счетом на «23» платит 120 без оклада на этом же договоре —
    # правило в модели есть, а данных для него не было.
    account: Mapped[str | None] = mapped_column(String(60), default=None)
    # Подразделение договора: вторая ставка по той же должности открывается
    # только на договоре другого подразделения.
    department: Mapped[str | None] = mapped_column(String(200), default=None)
    priority: Mapped[str | None] = mapped_column(String(60), default=None)
    allow_main: Mapped[str | None] = mapped_column(String(10), default=None)
    allow_part_time: Mapped[str | None] = mapped_column(String(10), default=None)
    salary_deadline: Mapped[str | None] = mapped_column(String(20), default=None)
    allowance_deadline: Mapped[str | None] = mapped_column(String(20), default=None)
    source: Mapped[str | None] = mapped_column(String(300), default=None)
    # Из какого документа взята строка. По имени файла связь ненадежна:
    # документ можно загрузить повторно или удалить, и данные должны уйти
    # вместе с ним, а не остаться сиротами.
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), default=None)

    case: Mapped[Case] = relationship(back_populates="contracts")


class Substitution(Base):
    """Правило замещения должности: кого кем можно заменить.

    Часть нормативной базы организации, а не дела: правила задаются один раз
    и действуют для всех планов, пока не выйдет новая редакция. Поэтому
    ``case_id`` пуст — строка общая.

    Правило направленное, а не симметричное: главного инженера проекта можно
    заместить инженером, обратное неверно. Модель расчета пока оперирует
    симметричными окладными группами; направленные правила решатель читает
    отдельным листом «правила_замещения» входного файла — сервис кладет их
    туда при сборке расчета.
    """

    __tablename__ = "substitutions"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), default=None)
    position: Mapped[str] = mapped_column(String(200))
    replaced_by: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str | None] = mapped_column(String(300), default=None)
    # Из какого документа взята строка. По имени файла связь ненадежна:
    # документ можно загрузить повторно или удалить, и данные должны уйти
    # вместе с ним, а не остаться сиротами.
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), default=None)

    case: Mapped[Case] = relationship(back_populates="substitutions")


class Document(Base):
    """Загруженный документ. Содержимое на диске, здесь — учет."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Документ принадлежит организации, а не плану: договор на три года
    # обслуживает три плана, и перезагружать его в каждый незачем.
    case_id: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), default=None)
    name: Mapped[str] = mapped_column(String(300))
    path: Mapped[str] = mapped_column(String(500))
    size: Mapped[int] = mapped_column(Integer, default=0)
    uploaded: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    # что это оказалось: РКМ, структура цены, штатное расписание, приказ...
    kind: Mapped[str | None] = mapped_column(String(120), default=None)
    # ожидает | разобран | не распознан
    state: Mapped[str] = mapped_column(String(30), default="ожидает")
    parsed_by: Mapped[str | None] = mapped_column(String(120), default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    # Версии. Тот же файл загружают снова — исправленный, дополненный, за
    # новый год. Раньше это давало вторую запись рядом с первой, и обе
    # кормили расчет. Теперь новая запись заменяет прежнюю: та получает
    # состояние «заменен», ее строки уходят из реестра, файл остается для
    # истории. Цепочка версий — по supersedes_id.
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[int | None] = mapped_column(Integer, default=None)
    # «реестр» — общий документ организации, строки идут в реестр;
    # «план» — песочница этого плана: чат его видит, разбор дает только
    # предложения, в реестр ничего не пишется.
    scope: Mapped[str] = mapped_column(String(20), default="реестр")
    # Отпечаток содержимого (SHA-256). Строка реестра ссылается на документ;
    # отпечаток дает проверить, что документ с тех пор не подменяли, и
    # отличить исправленную редакцию от того же файла, загруженного дважды.
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    # Что документ дал реестру, когда строк со ссылкой на него нет: величины
    # справочника должностей (JSON).
    gave: Mapped[str | None] = mapped_column(Text, default=None)

    case: Mapped[Case] = relationship(back_populates="documents")


class Activity(Base):
    """Работа агента: одна строка на один запуск.

    Именно эта таблица дает экономисту картину «кто сейчас работает». Пишется
    в начале работы со статусом «идет» и закрывается по завершении, поэтому
    состояние переживает перезагрузку страницы и уход с нее.
    """

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    agent: Mapped[str] = mapped_column(String(40))      # ключ агента, см. agents.py
    title: Mapped[str] = mapped_column(String(200))     # что делает, человеческим языком
    state: Mapped[str] = mapped_column(String(20), default="идет")  # идет | готово | ошибка | ждет ответа
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    started: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    finished: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    seconds: Mapped[float | None] = mapped_column(Float, default=None)
    # Что подано на вход и что получилось, JSON. Для ГОЗ важно уметь показать
    # не только «сделано», но и на чем именно: сколько текста ушло в модель,
    # что она вернула, что из этого приняли и что отбросили.
    artifact: Mapped[str | None] = mapped_column(Text, default=None)

    case: Mapped[Case] = relationship(back_populates="activities")


class Question(Base):
    """Вопрос агента экономисту. Пока не отвечен — работа стоит."""

    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    agent: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    options: Mapped[str | None] = mapped_column(Text, default=None)  # JSON-список вариантов
    answer: Mapped[str | None] = mapped_column(Text, default=None)
    asked: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    answered: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)

    case: Mapped[Case] = relationship(back_populates="questions")


class Message(Base):
    """Лента дела: что сказал агент, что ответил экономист."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    document_id: Mapped[int | None] = mapped_column(Integer, default=None)
    who: Mapped[str] = mapped_column(String(20))        # агент | экономист | система
    agent: Mapped[str | None] = mapped_column(String(40), default=None)
    # Кому передано: у передач между агентами есть и отправитель, и получатель.
    to_agent: Mapped[str | None] = mapped_column(String(40), default=None)
    text: Mapped[str] = mapped_column(Text)
    payload: Mapped[str | None] = mapped_column(Text, default=None)  # JSON: таблица, дифф, файл
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    case: Mapped[Case] = relationship(back_populates="messages")


class Run(Base):
    """Прогон расчета: на чем считали и что вышло."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    settings: Mapped[str | None] = mapped_column(Text, default=None)   # JSON
    status: Mapped[str] = mapped_column(String(40), default="идет")    # идет | OPTIMAL | ...
    seconds: Mapped[float | None] = mapped_column(Float, default=None)
    input_path: Mapped[str | None] = mapped_column(String(500), default=None)
    result_path: Mapped[str | None] = mapped_column(String(500), default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)    # JSON: итоги для карточки
    # Состав расчета: документы (имя, версия, отпечаток, сколько строк дали)
    # и версии агентов на момент запуска. План ГОЗ должен отвечать на вопрос
    # «на основании чего» и через год, когда документы уже заменены.
    sources: Mapped[str | None] = mapped_column(Text, default=None)    # JSON
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    # Кто считает и жив ли он. Перезапуск сервера раньше помечал «прерван»
    # любой прогон со статусом «идет» — в том числе живой расчёт другого
    # процесса. Теперь у прогона есть исполнитель (хост:pid:метка) и
    # heartbeat, который тот обновляет, пока считает; брошенным признаётся
    # только прогон, чей heartbeat давно замолчал.
    executor: Mapped[str | None] = mapped_column(String(120), default=None)
    heartbeat: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    # Ключ операции от клиента: повтор той же команды возвращает тот же
    # прогон, а не второй расчёт; тот же ключ с другими данными — конфликт.
    operation_id: Mapped[str | None] = mapped_column(String(80), default=None)
    # Снимок прогона: хеш канонического JSON в хранилище артефактов. Пути
    # input_path/result_path остаются для интерфейса, но снимком считается
    # только это: путь можно перезаписать, хеш — нет.
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), default=None)

    case: Mapped[Case] = relationship(back_populates="runs")


engine = create_engine("sqlite:///" + DB_PATH, future=True,
                       connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


class LaborRow(Base):
    """Строка трудоемкости договора: план в чел.-мес. и средняя стоимость.

    Главные данные РКМ и Формы 9д, и до сих пор им негде было лежать: лист
    «трудоемкость_по_договорам» входного файла оставался пустым, а весь раздел
    модели про закрытие плановых чел.-мес. работал вхолостую.
    """

    __tablename__ = "labor_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_code: Mapped[str] = mapped_column(String(60))
    year: Mapped[int | None] = mapped_column(Integer, default=None)
    position: Mapped[str | None] = mapped_column(String(200), default=None)
    salary_page: Mapped[str | None] = mapped_column(String(60), default=None)
    salary_group: Mapped[int | None] = mapped_column(Integer, default=None)
    position_level: Mapped[int | None] = mapped_column(Integer, default=None)
    person_months: Mapped[float | None] = mapped_column(Float, default=None)
    avg_cost: Mapped[float | None] = mapped_column(Float, default=None)
    # Число привлекаемых специалистов из РКМ — предел людей на строке в месяц.
    headcount: Mapped[float | None] = mapped_column(Float, default=None)
    # План по месяцам, JSON {"6": 1.0, …}: этап «4 чел.-мес. июнь–сентябрь»
    # из расшифровки ФОТ разложен по месяцам. Пусто — строка годовая.
    months: Mapped[str | None] = mapped_column(Text, default=None)
    # Исходные строки формы до объединения по должности: этап, вид работ,
    # даты, сумма и место в документе. Оптимизатор читает агрегат выше, а эта
    # расшифровка нужна для проверки полноты извлечения.
    details: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[str | None] = mapped_column(String(300), default=None)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), default=None)


class Inflow(Base):
    """Поступление денег по договору за месяц.

    Решатель не может потратить деньги раньше, чем они поступили. Раньше
    помесячная разбивка бралась из шаблона и масштабировалась под фонд —
    то есть попросту выдумывалась.
    """

    __tablename__ = "inflows"

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_code: Mapped[str] = mapped_column(String(60))
    year: Mapped[int | None] = mapped_column(Integer, default=None)
    month: Mapped[int] = mapped_column(Integer)
    amount: Mapped[float | None] = mapped_column(Float, default=None)
    source: Mapped[str | None] = mapped_column(String(300), default=None)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), default=None)


class SecretAllowance(Base):
    """Кому платится 120 и какой договор задает период секретности."""

    __tablename__ = "secret_allowances"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_code: Mapped[str] = mapped_column(String(60))
    secret_contract_code: Mapped[str | None] = mapped_column(String(60), default=None)
    rate: Mapped[float | None] = mapped_column(Float, default=None)
    source: Mapped[str | None] = mapped_column(String(300), default=None)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), default=None)


class Correction(Base):
    """Правка экономиста: что агент прочитал неверно и как верно.

    Это память агента. Перед разбором документа того же вида последние правки
    подмешиваются в подсказку — «в таких документах оклад стоит в графе
    „Должностной оклад с учетом ПК“, а не „Базовый оклад“», — и агент не
    повторяет ошибку, пока не сменится подсказка. Одновременно правка — случай
    для стенда через приговор.
    """

    __tablename__ = "corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_name: Mapped[str] = mapped_column(String(300))
    document_kind: Mapped[str | None] = mapped_column(String(120), default=None)
    entity: Mapped[str | None] = mapped_column(String(40), default=None)
    wrong: Mapped[str | None] = mapped_column(Text, default=None)   # JSON или текст
    right: Mapped[str | None] = mapped_column(Text, default=None)   # JSON или текст
    note: Mapped[str | None] = mapped_column(Text, default=None)    # словами экономиста
    agent_version: Mapped[str | None] = mapped_column(String(20), default=None)
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Verdict(Base):
    """Приговор экономиста предложенной строке: принята или отклонена.

    Это и есть самообучение в честном виде. Модель не переучивается — но
    каждый приговор становится случаем стенда: принятая строка обязана
    извлекаться из этого документа и впредь, отклоненная — не должна
    повториться слово в слово. Меняется подсказка агента — стенд прогоняет
    все накопленные приговоры и говорит, что сломалось. Чем дольше сервисом
    пользуются, тем строже он себя проверяет.
    """

    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_name: Mapped[str] = mapped_column(String(300))
    entity: Mapped[str] = mapped_column(String(40))
    fields: Mapped[str] = mapped_column(Text)          # JSON строки предложения
    verdict: Mapped[str] = mapped_column(String(20))   # принято | отклонено
    agent_version: Mapped[str | None] = mapped_column(String(20), default=None)
    # Оценка, которую строка получила до приговора: по ней стенд считает
    # калибровку — сколько «надежных» экономист отклонил.
    grade: Mapped[str | None] = mapped_column(String(20), default=None)
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Proposal(Base):
    """Строка, вычитанная моделью из документа неизвестной формы, — до
    подтверждения экономистом.

    Лежит отдельно от реестра нарочно. Пока строка не подтверждена, она
    физически не может попасть ни в реестр, ни в паспорт дела, ни во входной
    файл решателя: ее там просто нет. Для ГОЗ это и есть разница между «ИИ
    помог разобрать» и «ИИ подсунул правдоподобное»: в расчет уходит только
    то, что человек посмотрел и принял.

    ``evidence`` — откуда строка взята в документе: лист и номер строки, адрес
    ячейки или цитата. Без этого предложение нечем проверить, кроме как искать
    значение в файле глазами.
    """

    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"))
    # сотрудник | договор | правило замещения
    entity: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(String(400), default=None)
    # Насколько строке можно верить — «надежно», «проверить», «сомнительно» —
    # и почему. Это не самооценка модели (та уверена всегда одинаково), а
    # итог проверок по реестру и справочнику; см. intake.grade.
    grade: Mapped[str | None] = mapped_column(String(20), default=None)
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    # предложено | принято | отклонено
    state: Mapped[str] = mapped_column(String(20), default="предложено")
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Decision(Base):
    """Локальное решение: кто, что, над какой версией предмета и почему.

    Принятая строка, записанная норма, исключённый документ, условие плана
    из чата — решения человека. От них оставались только следствия; теперь
    есть запись с оператором, хешем предмета и предусловием (VER-004).
    """

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    actor: Mapped[str | None] = mapped_column(String(120), default=None)
    actor_source: Mapped[str | None] = mapped_column(String(60), default=None)
    kind: Mapped[str] = mapped_column(String(40))           # данные | норма | состав плана | настройка плана
    subject_kind: Mapped[str] = mapped_column(String(40))   # документ | план
    subject_id: Mapped[str] = mapped_column(String(80))
    subject_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    scope: Mapped[str | None] = mapped_column(String(120), default=None)
    action: Mapped[str | None] = mapped_column(Text, default=None)      # JSON
    grounds: Mapped[str | None] = mapped_column(Text, default=None)
    precondition: Mapped[str | None] = mapped_column(String(64), default=None)
    outcome: Mapped[str | None] = mapped_column(String(120), default=None)


def record_decision(db, **fields):
    """Записать решение; действующее лицо берётся у локального оператора,
    а не из аргументов — подпись клиента личностью не является (NFR-002)."""
    from fot_planner.harness_local import decisions as _d
    row = Decision(**_d.make(**fields))
    db.add(row)
    return row


#: Колонки, добавленные к уже существующим таблицам. create_all создает
#: недостающие таблицы, но не колонки, а базу с делами экономиста мы не
#: пересоздаем. SQLite умеет ADD COLUMN, этого достаточно.
_ADDED_COLUMNS = {
    # Переписка по документу живет в тех же сообщениях, что и лента плана,
    # только с ссылкой на документ вместо плана.
    "messages": [("document_id", "INTEGER")],
    "cases": [("muted_docs", "TEXT"), ("plan_settings", "TEXT")],
    "documents": [("version", "INTEGER"), ("supersedes_id", "INTEGER"),
                  ("scope", "VARCHAR(20)"),
                  ("sha256", "VARCHAR(64)"),
                  # Что документ дал реестру, когда это не строки со ссылкой
                  # на него: величины справочника должностей.
                  ("gave", "TEXT")],
    "proposals": [("grade", "VARCHAR(20)"), ("reason", "TEXT")],
    "runs": [("sources", "TEXT"), ("executor", "VARCHAR(120)"),
             ("heartbeat", "DATETIME"), ("operation_id", "VARCHAR(80)"),
             ("manifest_sha256", "VARCHAR(64)")],
    "labor_rows": [("headcount", "FLOAT"), ("months", "TEXT"),
                   ("details", "TEXT")],
    "verdicts": [("grade", "VARCHAR(20)")],
    "employees": [
        ("person_code", "VARCHAR(60)"),
        ("department", "VARCHAR(200)"),
        ("employment_type", "VARCHAR(40)"),
        ("employment_category", "VARCHAR(40)"),
        ("allowed_contracts", "VARCHAR(300)"),
        ("forbidden_contracts", "VARCHAR(300)"),
    ],
    "contracts": [
        ("account", "VARCHAR(60)"),
        ("department", "VARCHAR(200)"),
        ("priority", "VARCHAR(60)"),
        ("allow_main", "VARCHAR(10)"),
        ("allow_part_time", "VARCHAR(10)"),
        ("salary_deadline", "VARCHAR(20)"),
        ("allowance_deadline", "VARCHAR(20)"),
    ],
}


def init_db():
    Base.metadata.create_all(engine)
    from sqlalchemy import text

    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            have = {r[1] for r in conn.execute(text("PRAGMA table_info(%s)" % table))}
            for name, kind in columns:
                if name not in have:
                    conn.execute(text("ALTER TABLE %s ADD COLUMN %s %s"
                                      % (table, name, kind)))


def session():
    return SessionLocal()
