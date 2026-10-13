"""CHAT-DOC-001: разговор о документе правит строки ожидания, а не реестр.

Случай 12.10.2026 (песочница на Qwen): на «ты забыл ГОЗ» агент писал правку
мимо предложения в реестр и заводил там по пустому договору на каждую графу,
на «шифр неверный» переспрашивал, не видя соседних документов, а на «да,
только оклад» слово в слово повторял прошлый ответ.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'harness'))


class DocChatProposalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from lifecycle import App
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, FOT_OPERATOR='doc-chat-test-operator')
        cls.env.start()
        cls.app = App(cls.temp.name)
        import chat
        import llm
        cls.chat, cls.llm = chat, llm

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.app.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db, self.d = self.app.db, self.app.d
        path = Path(self.temp.name) / (self._testMethodName + '.txt')
        path.write_text('Источник финансирования: внебюджет', encoding='utf-8')
        self.doc = self.d.Document(name=path.name, path=str(path),
                                   sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                   kind='документ по договору', case_id=self.app.case.id,
                                   scope='реестр')
        self.db.add(self.doc)
        self.db.commit()
        self.code = 'НИР-ТЕСТ-' + str(self.doc.id)
        self.pr = self.d.Proposal(document_id=self.doc.id, entity='договор',
                                  payload=json.dumps({'code': self.code, 'goz': None,
                                                      'fot': 1250000, 'from': None},
                                                     ensure_ascii=False),
                                  evidence='лист «карточка», B6', grade='проверить')
        self.db.add(self.pr)
        self.db.commit()

    def say(self, edits, text='ты забыл ГОЗ'):
        from fastapi import BackgroundTasks
        reply = {'ok': True, 'reply': 'исправляю', 'action': 'заполнить поле', 'edits': edits}
        with patch.object(self.app.main.chat, 'reply_doc', return_value=reply):
            self.app.main.document_message(self.doc.id, BackgroundTasks(), {'text': text})
        self.db.expire_all()

    def contracts(self):
        return self.db.query(self.d.Contract).filter_by(code=self.code).count()

    def test_edit_by_number_changes_pending_row_not_registry(self):
        self.say([{'entity': 'договор', 'key': 'п%d' % self.pr.id, 'field': 'ГОЗ', 'value': 'нет'},
                  {'entity': 'договор', 'key': 'п%d' % self.pr.id, 'field': 'действует с',
                   'value': '01.04.2026'}])
        f = json.loads(self.db.get(self.d.Proposal, self.pr.id).payload)
        self.assertEqual((f['goz'], f['from']), ('нет', '01.04.2026'))
        self.assertEqual(self.contracts(), 0)

    def test_edit_by_code_routes_to_pending_row(self):
        self.say([{'entity': 'договор', 'key': self.code, 'field': 'разрешенные выплаты',
                   'value': 'только оклад'}])
        f = json.loads(self.db.get(self.d.Proposal, self.pr.id).payload)
        self.assertEqual(f['allow_salary'], 'да')
        self.assertEqual({f[k] for k in ('allow_120', 'allow_122', 'allow_124',
                                         'allow_152', 'allow_order')}, {'нет'})
        self.assertEqual(self.contracts(), 0)

    def test_employment_is_a_closed_list(self):
        # «Совмещение» — не совместительство; «совместитель» — совместительство.
        v = self.chat._proposal_value
        self.assertEqual(v('employment', 'совместитель'), 'совместительство')
        self.assertEqual(v('employment', 'основное место'), 'основное')
        self.assertIsNone(v('employment', 'совмещение'))

    def test_unknown_contract_is_not_created_from_document_chat(self):
        self.say([{'entity': 'договор', 'key': 'НЕТ-ТАКОГО', 'field': 'ГОЗ', 'value': 'да'}])
        self.assertEqual(self.db.query(self.d.Contract).filter_by(code='НЕТ-ТАКОГО').count(), 0)
        last = self.db.query(self.d.Message).filter_by(document_id=self.doc.id) \
            .order_by(self.d.Message.id.desc()).first()
        self.assertIn('править нечего', last.text)

    def test_plan_chat_creates_one_contract_for_several_fields(self):
        key = 'НОВЫЙ-' + str(self.doc.id)
        self.chat.apply_edits(self.db, None, [
            {'entity': 'договор', 'key': key, 'field': f, 'value': v}
            for f, v in (('ГОЗ', 'нет'), ('действует с', '01.04.2026'),
                         ('действует по', '30.09.2026'))])
        self.assertEqual(self.db.query(self.d.Contract).filter_by(code=key).count(), 1)

    def test_doc_context_shows_pending_rows_and_known_contracts(self):
        self.db.add(self.d.Contract(code='ГРН-26/04', name='Гранит-М'))
        self.db.commit()
        text = self.chat.doc_context(self.db, self.doc)
        self.assertIn('п%d (договор)' % self.pr.id, text)
        self.assertIn('fot=1250000', text)
        self.assertIn('ГРН-26/04 Гранит-М', text)

    def test_repeated_reply_is_asked_again(self):
        self.db.add(self.d.Message(case_id=self.app.case.id, document_id=self.doc.id,
                                   who='агент', text='Назовите правильную сумму в тысячах рублей.'))
        self.db.commit()
        answers = [{'reply': 'Назовите правильную сумму в тысячах рублей.', 'action': 'ничего'},
                   {'reply': 'Оклад 35 500: 71 000 за ставку × 0,5.', 'action': 'заполнить поле',
                    'edits': [{'entity': 'сотрудник', 'key': 'п1', 'field': 'оклад', 'value': '?'}]}]
        with patch.object(self.llm, 'provider', return_value=('local', 'm', '')), \
                patch.object(self.chat, '_ask_doc', side_effect=answers) as ask:
            res = self.chat.reply_doc(self.db, self.doc, 'да, только оклад')
        self.assertEqual(ask.call_count, 2)
        self.assertIn('Не повторяй', ask.call_args.args[2])
        self.assertTrue(res['reply'].startswith('Оклад 35 500'))
        self.assertEqual(res['edits'], [])          # значение «?» — не правка

    def test_card_polling_starts_one_warmup_per_document(self):
        # Каждый опрос карточки заводил поток печати через COM: прогон
        # пятидесяти документов ронял сервер с 0xC0000005 (12.10.2026).
        import threading
        gate, calls = threading.Event(), []

        def slow(x):
            calls.append(x)
            gate.wait(5)
        main = self.app.main
        for _ in range(20):
            main._warm_once(('pdf', 'тест'), slow, 1)
        gate.set()
        for _ in range(50):
            if ('pdf', 'тест') not in main._WARMING:
                break
            threading.Event().wait(0.05)
        main._warm_once(('pdf', 'тест'), slow, 2)
        threading.Event().wait(0.2)
        self.assertEqual(calls, [1, 2])

    def test_office_printing_runs_in_child_process(self):
        # COM в потоке сервера ронял весь процесс (0xC0000005, 12.10.2026).
        main = self.app.main
        with patch.object(main.subprocess, 'run') as run, \
                patch.dict(sys.modules, {'win32com': object()}):
            self.assertFalse(main._office_to_pdf('a.docx', str(Path(self.temp.name) / 'нет.pdf')))
        args = run.call_args.args[0]
        self.assertTrue(args[1].endswith('office_pdf.py'))
        self.assertEqual(args[2:], ['a.docx', str(Path(self.temp.name) / 'нет.pdf')])

    def test_claimed_fix_without_edit_is_asked_again(self):
        # «Исправляю на 950 000» без правки — переспрос; второй ответ с правкой.
        answers = [{'reply': 'Разборщик записал 950 000 000, исправляю на 950 000.', 'action': 'ничего'},
                   {'reply': 'Фонд 950 000: «0,95 млн руб».', 'action': 'заполнить поле',
                    'edits': [{'entity': 'договор', 'key': 'п1', 'field': 'фонд', 'value': '950000'}]}]
        with patch.object(self.llm, 'provider', return_value=('local', 'm', '')), \
                patch.object(self.chat, '_ask_doc', side_effect=answers) as ask:
            res = self.chat.reply_doc(self.db, self.doc, 'фонд неверный')
        self.assertEqual(ask.call_count, 2)
        self.assertEqual(res['edits'][0]['value'], '950000')

    def test_question_with_edit_does_not_write(self):
        # Ответ спрашивает «уточните», а правка меняет верную зарплату.
        answer = {'reply': 'Зарплата 10 500 = 42 000 × 0,25. Если иначе, уточните сумму.',
                  'action': 'заполнить поле',
                  'edits': [{'entity': 'сотрудник', 'key': 'п1', 'field': 'оклад', 'value': '42000'}]}
        with patch.object(self.llm, 'provider', return_value=('local', 'm', '')), \
                patch.object(self.chat, '_ask_doc', return_value=answer):
            res = self.chat.reply_doc(self.db, self.doc, 'зарплата неверная')
        self.assertEqual(res['edits'], [])

    def test_goz_edit_against_document_sign_is_dropped(self):
        # На ложное «ГОЗ неверный» Qwen снимал ГОЗ, цитируя «заказчик —
        # Министерство обороны» (прогон 50 документов, 12.10.2026).
        answer = {'reply': 'Это госзаказ, а не ГОЗ. Исправляю на «нет».', 'action': 'заполнить поле',
                  'edits': [{'entity': 'договор', 'key': 'п1', 'field': 'ГОЗ', 'value': 'нет'}]}
        text = 'РЕШИЛИ: Государственный заказчик — Министерство обороны Российской Федерации.'
        with patch.object(self.llm, 'provider', return_value=('local', 'm', '')), \
                patch.object(self.chat, '_ask_doc', return_value=answer), \
                patch.object(self.chat.docread, 'to_text', return_value=text):
            res = self.chat.reply_doc(self.db, self.doc, 'ГОЗ определён неправильно.')
            self.assertEqual(res['edits'], [])
            self.assertIn('Министерство обороны', res['reply'])
            said = self.chat.reply_doc(self.db, self.doc, 'Это не ГОЗ, поставь нет.')
        self.assertEqual(len(said['edits']), 1)          # прямое слово экономиста главнее

    def test_salary_is_not_divided_back_by_rate_without_a_number(self):
        # 24 500 при ставке 0,5 агент «исправлял» на 49 000 «за ставку».
        pr = self.d.Proposal(document_id=self.doc.id, entity='сотрудник', evidence='строка 8',
                             payload=json.dumps({'code': '1', 'fio': 'Козлов О. Г.', 'rate': 0.5,
                                                 'salary': 24500}, ensure_ascii=False))
        self.db.add(pr)
        self.db.commit()
        edit = [{'entity': 'сотрудник', 'key': 'п%d' % pr.id, 'field': 'оклад', 'value': '49000'}]
        self.assertEqual(self.chat._rate_guard(self.db, self.doc, 'зарплата неверная', edit), [])
        self.assertIn('24', self.chat._rate_guard.note)
        self.assertEqual(self.chat._rate_guard(self.db, self.doc, 'у него 49 000', edit), edit)

    def test_replacement_needs_evidence_in_document_or_words(self):
        # На ложное «неверно» модель выдумывала новое значение заполненного поля.
        pr = self.d.Proposal(document_id=self.doc.id, entity='договор', evidence='п. 3',
                             payload=json.dumps({'code': 'Т-1', 'fot': 4800000, 'from': '01.04.2026',
                                                 'to': None}, ensure_ascii=False))
        self.db.add(pr)
        self.db.commit()
        key = 'п%d' % pr.id
        text = ('Фонд — четыре миллиона восемьсот тысяч рублей, резерв 5 200 тыс. руб. '
                'Срок — II–III кварталы 2026 г.')
        guard = self.chat._evidence_guard

        def ed(field, value):
            return [{'entity': 'договор', 'key': key, 'field': field, 'value': value}]
        with patch.object(self.chat.docread, 'to_text', return_value=text):
            self.assertEqual(guard(self.db, self.doc, 'фонд неверный', ed('фонд', '4 300 000')), [])
            self.assertIn('4 800 000', guard.note)
            self.assertEqual(len(guard(self.db, self.doc, 'фонд неверный', ed('фонд', '5200000'))), 1)
            self.assertEqual(len(guard(self.db, self.doc, 'фонд 4,3 млн', ed('фонд', '4300000'))), 1)
            self.assertEqual(guard(self.db, self.doc, 'срок не тот', ed('действует с', '15.05.2026')), [])
            self.assertEqual(len(guard(self.db, self.doc, 'срок не тот', ed('действует с', '01.07.2026'))), 1)
            self.assertEqual(len(guard(self.db, self.doc, 'нет срока', ed('действует по', '20.11.2026'))), 1)
        self.assertEqual(self.chat._numbers('один миллион двести пятьдесят тысяч рублей'), [1250000])

    def test_part_time_edit_against_document_phrase_is_dropped(self):
        # «Внутренние совместители — можно», а агент «исправил» на «нет».
        guard = self.chat._goz_guard
        ed = [{'entity': 'договор', 'key': 'п1', 'field': 'совместительство', 'value': 'нет'}]
        with patch.object(self.chat.docread, 'to_text',
                          return_value='Совместители | Внутренние совместители — можно.'):
            self.assertEqual(guard(self.doc, 'про совместителей неверно', ed), [])
            self.assertIn('можно', guard.note)
            self.assertEqual(guard(self.doc, 'совместителей нельзя, решили на совещании', ed), ed)
        with patch.object(self.chat.docread, 'to_text',
                          return_value='Совместителей к работам не привлекать.'):
            yes = [dict(ed[0], value='да')]
            self.assertEqual(guard(self.doc, 'неверно', yes), [])
            self.assertEqual(guard(self.doc, 'неверно', ed), ed)

    def test_noop_edit_is_not_blocked_as_unbacked(self):
        pr = self.d.Proposal(document_id=self.doc.id, entity='сотрудник', evidence='строка 5',
                             payload=json.dumps({'code': '7', 'rate': 1.0, 'salary': 31500},
                                                ensure_ascii=False))
        self.db.add(pr)
        self.db.commit()
        ed = [{'entity': 'сотрудник', 'key': 'п%d' % pr.id, 'field': 'оклад', 'value': '31500'}]
        with patch.object(self.chat.docread, 'to_text', return_value='Петров | 0,25 | 126'):
            self.assertEqual(self.chat._evidence_guard(self.db, self.doc, 'ставка не та', ed), ed)
        # «$2,300,000» — то же число, а не дробь 2,3.
        self.assertEqual(self.chat._num('$2,300,000'), 2300000.0)
        self.assertEqual(self.chat._num('1,5'), 1.5)

    def test_empty_fields_are_shown_as_empty(self):
        text = self.chat.doc_context(self.db, self.doc)
        self.assertIn('goz=пусто', text)
        self.assertIn('from=пусто', text)

    def test_broken_schema_answer_is_asked_without_schema(self):
        calls = []

        def fake(*args, **kw):
            calls.append(kw['schema'])
            if kw['schema']:
                raise ValueError('Expecting value: line 2443 column 2')
            return {'reply': 'ок', 'action': 'ничего'}
        with patch.object(self.llm, '_use_schema', return_value=True), \
                patch.object(self.llm, '_call_openai_compatible', side_effect=fake), \
                patch.dict(os.environ, FOT_LLM_BASE_URL='http://127.0.0.1:9'):
            raw = self.chat._ask_doc('local', 'm', 'вопрос', 'файл')
        self.assertEqual((calls, raw['reply']), ([True, False], 'ок'))


class StreamTests(unittest.TestCase):
    """Ответ модели читается потоком: длинное тело одним куском не доходило."""

    def test_sse_chunks_are_joined(self):
        sys.path.insert(0, str(ROOT / 'app'))
        import llm
        body = (b'data: {"choices":[{"delta":{"content":"{\\"a\\": "}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"1}"},"finish_reason":"stop"}]}\n\n'
                b'data: [DONE]\n\n')

        class Resp(io.BytesIO):
            headers = {'Content-Type': 'text/event-stream'}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        req = llm.urllib.request.Request('http://127.0.0.1:9/chat/completions', method='POST')
        with patch.object(llm, '_open', return_value=Resp(body)) as op:
            text, finish = llm._post_chat(req, {'model': 'm'}, None)
        self.assertEqual((text, finish), ('{"a": 1}', 'stop'))
        self.assertTrue(json.loads(op.call_args.args[0].data)['stream'])

    def test_all_zero_months_mean_no_breakdown(self):
        sys.path.insert(0, str(ROOT / 'app'))
        import llm
        row = {'contract': 'Г', 'position': 'техник', 'person_months': 6,
               'avg_cost': 58400, 'months': [0] * 12}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'письмо.docx'
            path.write_bytes(b'')
            with patch.object(llm, 'provider', return_value=('local', 'm', '')), \
                    patch.object(llm.docread, 'to_text', return_value='текст'), \
                    patch.object(llm, '_ask_freeform', return_value={'labor': [row]}):
                res = llm.freeform(str(path))
        self.assertIsNone(res['labor'][0]['months'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
