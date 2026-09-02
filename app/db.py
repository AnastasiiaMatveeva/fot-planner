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


def init_db():
    Base.metadata.create_all(engine)


def session():
    return SessionLocal()
