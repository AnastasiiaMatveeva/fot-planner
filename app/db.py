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
DATA_DIR = os.path.join(HERE, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
RESULT_DIR = os.path.join(DATA_DIR, "results")
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
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

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
    # предложено | принято | отклонено
    state: Mapped[str] = mapped_column(String(20), default="предложено")
    created: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


#: Колонки, добавленные к уже существующим таблицам. create_all создает
#: недостающие таблицы, но не колонки, а базу с делами экономиста мы не
#: пересоздаем. SQLite умеет ADD COLUMN, этого достаточно.
_ADDED_COLUMNS = {
    "employees": [
        ("department", "VARCHAR(200)"),
        ("employment_type", "VARCHAR(40)"),
        ("employment_category", "VARCHAR(40)"),
        ("allowed_contracts", "VARCHAR(300)"),
        ("forbidden_contracts", "VARCHAR(300)"),
    ],
    "contracts": [
        ("account", "VARCHAR(60)"),
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
