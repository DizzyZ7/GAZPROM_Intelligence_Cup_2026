"""Приемка контекстного разбора в рабочем API и накопительном импорте."""

import csv
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from evidence import get_signal
from pulse import Ticket
from service import ingest_batch
from store import SignalStore

PACKET = (
    Path(__file__).resolve().parents[1]
    / "reviews/pulse-synthetic-v1-20261109/review.csv"
)


class SameVector:
    def __init__(self):
        self.seen = []

    def embed(self, texts):
        self.seen.extend(texts)
        return [[1.0, 0.0] for _ in texts]


class ProductContextTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = SignalStore(Path(folder.name) / "pulse.db")
        self.addCleanup(self.store.close)

    def cards(self):
        return [get_signal(self.store, row["id"]) for row in self.store.list_signals()]

    def test_human_feedback_is_used_without_gold_fields(self):
        with PACKET.open(encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        selected = (2, 5, 8, 16, 22, 28, 38)
        tickets = [
            Ticket(
                rows[n - 1]["ticket_id"],
                rows[n - 1]["product_id"],
                rows[n - 1]["text"],
                rows[n - 1]["source_channel"],
            )
            for n in selected
        ]
        result = ingest_batch(self.store, tickets)
        classified = {row["id"]: row for row in result.get("classified", [])}
        expected = {
            2: ("bug", "report_download"),
            5: ("feature_request", "contact_edit"),
            8: ("feature_request", "contact_edit"),
            16: ("unknown", "unclassified"),
            22: ("service_request", "permissions"),
            28: ("feature_request", "contact_edit"),
            38: ("bug", "contract_download"),
        }
        for n, values in expected.items():
            with self.subTest(row=n):
                prediction = classified.get(rows[n - 1]["ticket_id"], {})
                self.assertEqual(
                    (prediction.get("category"), prediction.get("workflow")), values
                )
        report = classified.get(rows[1]["ticket_id"], {})
        self.assertEqual(report.get("symptom"), "not_found")
        self.assertNotIn("404", str(report))
        (contact,) = self.cards()
        self.assertEqual(
            set(contact["ticket_ids"]), {rows[4]["ticket_id"], rows[7]["ticket_id"]}
        )

    def test_contact_needs_do_not_merge_even_with_identical_embeddings(self):
        cases = [
            ("replace-a", "Хочу изменять контактный номер в профиле без оператора."),
            ("replace-b", "Добавьте возможность самому поменять телефон для связи."),
            (
                "many-a",
                "Добавьте несколько телефонов для связи: личный и рабочий одновременно.",
            ),
            (
                "many-b",
                "Нужен второй контактный номер в профиле, менять основной не хочу.",
            ),
        ]
        for mode in ("rules", "semantic"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                with closing(SignalStore(Path(folder) / "pulse.db")) as store:
                    ingest_batch(
                        store,
                        [Ticket(i, "P", text) for i, text in cases],
                        mode=mode,
                        provider=SameVector(),
                    )
                    groups = {
                        frozenset(get_signal(store, row["id"])["ticket_ids"])
                        for row in store.list_signals()
                    }
                    self.assertEqual(
                        groups,
                        {
                            frozenset({"replace-a", "replace-b"}),
                            frozenset({"many-a", "many-b"}),
                        },
                    )

    def test_download_object_and_observed_status_both_constrain_groups(self):
        cases = [
            ("r1", "Готовый отчет не скачивается: Not Found."),
            ("r2", "При скачивании отчета вижу Not Found, данные не получить."),
            ("r404-a", "Скачивание отчета возвращает HTTP 404."),
            ("r404-b", "Отчет не скачивается: ошибка 404."),
            ("c1", "Подписанный договор не скачивается: ошибка 404."),
            ("c2", "При скачивании договора получаю HTTP 404."),
        ]
        ingest_batch(
            self.store,
            [Ticket(i, "P", text) for i, text in cases],
            mode="semantic",
            provider=SameVector(),
        )
        self.assertEqual(
            {frozenset(card["ticket_ids"]) for card in self.cards()},
            {
                frozenset({"r1", "r2"}),
                frozenset({"r404-a", "r404-b"}),
                frozenset({"c1", "c2"}),
            },
        )

    def test_unknown_and_service_requests_are_not_sent_for_embedding(self):
        provider = SameVector()
        result = ingest_batch(
            self.store,
            [
                Ticket(
                    "service",
                    "P",
                    "Оформите заявку на права к отчетам. Ограничение соответствует моей роли.",
                ),
                Ticket(
                    "mixed",
                    "P",
                    "Очередь ошибка 404. И отдельно, это другая задача: хочу второй номер телефона.",
                ),
            ],
            mode="semantic",
            provider=provider,
        )
        self.assertEqual(provider.seen, [])
        self.assertEqual(result["signals_found"], 0)
        self.assertEqual(len(self.store.staged_tickets()), 2)

    def test_context_is_exposed_as_evidence_and_id_survives_growth(self):
        initial = [
            Ticket(i, "P", "Добавьте несколько телефонов для связи одновременно.")
            for i in ("b", "c")
        ]
        ingest_batch(self.store, initial)
        (card,) = self.cards()
        self.assertEqual(
            card.get("context"),
            {"operation": "contact_edit", "symptom": "multiple_contacts"},
        )
        self.assertTrue(card.get("analysis_version"))
        self.store.review(
            card["id"],
            "approved",
            "analyst",
            "Проверены два источника",
            expected_revision=card["revision"],
        )
        snapshot = self.store.queue()
        ingest_batch(self.store, [Ticket("a", "P", initial[0].text)])
        (grown,) = self.cards()
        self.assertEqual(grown["id"], card["id"])
        self.assertEqual(set(grown["ticket_ids"]), {"a", "b", "c"})
        self.assertEqual(self.store.queue(), snapshot)

    def test_legacy_mixed_signal_is_not_silently_expanded_or_split(self):
        old = [
            Ticket("replace", "P", "Хочу изменять контактный номер в профиле."),
            Ticket("many", "P", "Добавьте несколько телефонов для связи одновременно."),
        ]
        self.store.stage_tickets(old)
        self.store.ingest(
            [
                {
                    "id": "legacy",
                    "product": "P",
                    "category": "feature_request",
                    "workflow": "contact_edit",
                    "ticket_ids": ["replace", "many"],
                    "ticket_count": 2,
                    "examples": [],
                }
            ]
        )
        before = get_signal(self.store, "legacy")
        self.store.review(
            "legacy",
            "approved",
            "analyst",
            "Старое решение",
            expected_revision=before["revision"],
        )
        before = get_signal(self.store, "legacy")
        queue = self.store.queue()
        with self.assertRaisesRegex(ValueError, "согласован|провер|контекст"):
            ingest_batch(self.store, [Ticket("new", "P", old[1].text)])
        self.assertEqual(get_signal(self.store, "legacy"), before)
        self.assertEqual(self.store.queue(), queue)
        self.assertEqual(self.store.staged_tickets(), sorted(old, key=lambda t: t.id))

    def test_questions_and_negated_requests_do_not_become_features(self):
        cases = [
            (
                "how-contact",
                "Как выполнить самостоятельную смену контактного телефона?",
                "how_to",
            ),
            (
                "no-feature",
                "Самостоятельная смена контактного телефона не нужна.",
                "unknown",
            ),
            ("how-access", "Как оформить заявку на права к отчетам?", "how_to"),
            ("how-error", "Как скачать отчет, если появится Not Found?", "how_to"),
        ]
        result = ingest_batch(
            self.store, [Ticket(i, "P", text) for i, text, _ in cases]
        )
        actual = {row["id"]: row["category"] for row in result["classified"]}
        self.assertEqual(actual, {i: expected for i, _, expected in cases})
        self.assertEqual(self.cards(), [])

    def test_bare_status_at_end_of_booking_result_is_not_lost(self):
        result = ingest_batch(
            self.store,
            [
                Ticket(
                    "a", "P", "При подтверждении бронирования очереди вижу ошибку 404."
                ),
                Ticket(
                    "b",
                    "P",
                    "В очереди выбираю слот, подтверждаю бронирование, выходит 404.",
                ),
            ],
        )
        self.assertEqual(result["signals_found"], 1)
        (card,) = self.cards()
        self.assertEqual(card["ticket_ids"], ["a", "b"])

    def test_declined_contact_edit_and_mixed_workflow_require_review(self):
        texts = [
            "Можно сделать несколько контактных телефонов? Самостоятельная смена номера мне не нужна.",
            "Можно сделать второй контактный телефон в профиле? Самостоятельная смена номера не требуется.",
            "Можно сделать самостоятельную смену контактного телефона и пароля?",
            "Можно сделать самостоятельную смену контактного телефона и оплату по QR?",
        ]
        result = ingest_batch(
            self.store, [Ticket(str(i), "P", text) for i, text in enumerate(texts)]
        )
        self.assertEqual(self.cards(), [])
        self.assertTrue(all(row["needs_review"] for row in result["classified"]))

    def test_legacy_sources_cannot_move_to_another_workflow_without_agreement(self):
        from pulse import analyze

        old = [
            Ticket(i, "P", "Кнопка скачивания договора не реагирует на нажатие.")
            for i in ("old1", "old2")
        ]
        self.store.stage_tickets(old)
        self.store.ingest(analyze(old)["signals"])
        (before,) = self.cards()
        self.store.review(
            before["id"],
            "approved",
            "analyst",
            "Старое решение",
            expected_revision=before["revision"],
        )
        queue = self.store.queue()
        with self.assertRaisesRegex(ValueError, "согласован"):
            ingest_batch(self.store, [Ticket("new", "P", "Другая информация")])
        self.assertEqual(self.store.queue(), queue)
        self.assertEqual(len(self.cards()), 1)
        self.assertEqual(self.store.staged_tickets(), old)

    def test_explicit_numeric_not_found_keeps_the_observed_code(self):
        texts = [
            "Готовый отчет не скачивается: HTTP 404 Not Found.",
            "Готовый отчет не скачивается: 404 Not Found.",
        ]
        result = ingest_batch(
            self.store, [Ticket(str(i), "P", text) for i, text in enumerate(texts)]
        )
        self.assertEqual(result["signals_found"], 1)
        self.assertEqual({row["symptom"] for row in result["classified"]}, {"http_404"})

    def test_missing_page_paraphrases_group_without_inventing_http_code(self):
        tickets = [
            Ticket(
                "a",
                "P",
                "Выбираю время, жму записаться — страницы не существует. Талон не появился.",
            ),
            Ticket("b", "P", "После выбора слота пишет page not found."),
            Ticket("c", "P", "При бронировании очереди открывается 404."),
        ]
        result = ingest_batch(self.store, tickets)
        rows = {row["id"]: row for row in result["classified"]}
        self.assertEqual(rows["a"]["symptom"], "not_found")
        self.assertEqual(
            {frozenset(card["ticket_ids"]) for card in self.cards()},
            {frozenset({"a", "b"})},
        )
        self.assertEqual(rows["c"]["symptom"], "http_404")
        self.assertNotIn("404", str(rows["a"]))

    def test_reserve_phone_is_distinct_from_replacement(self):
        result = ingest_batch(
            self.store,
            [
                Ticket(
                    "a",
                    "P",
                    "Существующий телефон оставить, но хотелось бы записать еще один запасной.",
                ),
                Ticket(
                    "b", "P", "Добавьте несколько телефонов для связи одновременно."
                ),
                Ticket("c", "P", "Хочу изменять контактный номер в профиле."),
            ],
        )
        self.assertEqual(
            {row["symptom"] for row in result["classified"] if row["id"] == "a"},
            {"multiple_contacts"},
        )
        self.assertEqual(
            {frozenset(card["ticket_ids"]) for card in self.cards()},
            {frozenset({"a", "b"})},
        )

    def test_booking_notification_needs_are_not_merged(self):
        cases = [
            (
                "a",
                "Хочу получать напоминание за час до приема по подтвержденному талону.",
            ),
            ("b", "Добавьте напоминание до приема, талон уже подтвержден."),
            ("c", "Хочу получать уведомление, когда появится подходящий слот."),
            ("d", "Добавьте уведомление о появлении свободного слота."),
            ("e", "Хочу получать уведомление, когда моя очередь уже близко."),
        ]
        result = ingest_batch(self.store, [Ticket(i, "P", text) for i, text in cases])
        self.assertEqual(
            {frozenset(card["ticket_ids"]) for card in self.cards()},
            {frozenset({"a", "b"}), frozenset({"c", "d"})},
        )
        self.assertTrue(
            all(row["category"] == "feature_request" for row in result["classified"])
        )

    def test_closed_case_is_a_support_action_and_missing_file_needs_review(self):
        result = ingest_batch(
            self.store,
            [
                Ticket(
                    "close", "P", "Спасибо за помощь, прошу закрыть предыдущую заявку."
                ),
                Ticket(
                    "file", "P", "Смотрите вложение, все там. Сам файл не прикрепился."
                ),
            ],
        )
        rows = {row["id"]: row for row in result["classified"]}
        self.assertEqual(rows["close"]["category"], "service_request")
        self.assertEqual(rows["file"]["category"], "unknown")
        self.assertTrue(rows["file"]["needs_review"])

    def test_legacy_split_below_minimum_still_requires_agreement(self):
        from pulse import analyze

        old = [
            Ticket("a", "P", "Хочу изменять контактный номер в профиле."),
            Ticket("b", "P", "Добавьте несколько телефонов для связи одновременно."),
        ]
        self.store.stage_tickets(old)
        self.store.ingest(analyze(old)["signals"])
        before = self.cards()
        with self.assertRaisesRegex(ValueError, "аналитик|согласован"):
            ingest_batch(self.store, [Ticket("new", "P", "Для сведения")])
        self.assertEqual(self.cards(), before)
        self.assertEqual(self.store.staged_tickets(), old)

    def test_legacy_sources_becoming_unknown_still_require_agreement(self):
        from pulse import analyze

        old = [
            Ticket(i, "P", "Очередь ошибка 404. Смотрите вложение, все там.")
            for i in ("a", "b")
        ]
        self.store.stage_tickets(old)
        self.store.ingest(analyze(old)["signals"])
        before = self.cards()
        with self.assertRaisesRegex(ValueError, "аналитик|согласован"):
            ingest_batch(self.store, [Ticket("new", "P", "Для сведения")])
        self.assertEqual(self.cards(), before)
        self.assertEqual(self.store.staged_tickets(), old)

    def test_declined_notifications_do_not_become_positive_feature_signals(self):
        result = ingest_batch(
            self.store,
            [
                Ticket("a", "P", "Не хочу получать напоминание до приема по талону."),
                Ticket(
                    "b",
                    "P",
                    "Не хочу получать уведомление, когда появится свободный слот.",
                ),
            ],
        )
        self.assertEqual(self.cards(), [])
        self.assertTrue(all(row["needs_review"] for row in result["classified"]))

    def test_missing_evidence_is_not_overridden_by_access_or_guidance(self):
        result = ingest_batch(
            self.store,
            [
                Ticket(
                    "a",
                    "P",
                    "Оформите заявку на доступ к отчетам. Смотрите вложение, исходная проблема там.",
                ),
                Ticket(
                    "b",
                    "P",
                    "Как выполнить самостоятельную смену контактного телефона? Смотрите вложение.",
                ),
            ],
        )
        self.assertTrue(
            all(
                row["category"] == "unknown" and row["needs_review"]
                for row in result["classified"]
            )
        )


if __name__ == "__main__":
    unittest.main()
