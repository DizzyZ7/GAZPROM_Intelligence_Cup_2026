"""Версии источника, атомарность и изменения продуктовых сигналов."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from evidence import get_signal
from pulse import Ticket
from service import ingest_batch
from store import SignalStore

ACTIVE = "При бронировании очереди ошибка 404, запись не создается."
RESOLVED = "При бронировании очереди была ошибка 404. Сбой исправили, теперь бронирование работает."


class TicketVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pulse.db"
        self.store = SignalStore(self.path)
        self.addCleanup(self.store.close)

    def update(self, text, version=2, ticket_id="a", **kwargs):
        return ingest_batch(
            self.store,
            [Ticket(ticket_id, "P", text)],
            ticket_versions={ticket_id: version},
            **kwargs,
        )

    def seed(self):
        ingest_batch(self.store, [Ticket(i, "P", ACTIVE) for i in ("a", "b", "c")])
        return get_signal(self.store, "P:bug:booking_queue")

    def test_immutable_history_replay_and_stale_conflict(self):
        self.seed()
        self.update(RESOLVED)
        history = self.store.ticket_history("a")
        self.assertEqual([r["version"] for r in history], [1, 2])
        self.assertEqual(history[0]["text"], ACTIVE)
        self.assertEqual(history[1]["text"], RESOLVED)
        events = self.store.signal_changes("P:bug:booking_queue")
        self.assertEqual(self.update(RESOLVED)["updated_tickets"], 0)
        self.assertEqual(self.store.ticket_history("a"), history)
        self.assertEqual(self.store.signal_changes("P:bug:booking_queue"), events)
        for text, version in [(ACTIVE, 1), (ACTIVE, 2)]:
            with self.assertRaises(ValueError):
                self.update(text, version)
        self.assertEqual(self.store.ticket_history("a"), history)

    def test_resolution_removes_only_current_source_preserves_snapshot(self):
        before = self.seed()
        self.store.review(
            before["id"],
            "approved",
            "analyst",
            "Проверено",
            expected_revision=before["revision"],
        )
        snapshot = self.store.queue()
        self.update(RESOLVED)
        after = get_signal(self.store, before["id"])
        self.assertEqual(after["ticket_ids"], ["b", "c"])
        self.assertEqual(after["ticket_count"], 2)
        self.assertNotEqual(after["revision"], before["revision"])
        self.assertTrue(after["requires_recheck"])
        self.assertEqual(self.store.queue(), snapshot)
        (event,) = self.store.signal_changes(before["id"])
        self.assertEqual((event["sources_before"], event["sources_after"]), (3, 2))
        self.assertEqual(event["removed_ticket_ids"], ["a"])
        self.assertEqual(event["updates"][0]["reason"], "resolved_issue")

    def test_new_symptom_changes_only_related_signal_and_can_rejoin(self):
        self.seed()
        ingest_batch(
            self.store,
            [Ticket(i, "Q", "Отчет не скачивается: ошибка 404.") for i in ("q1", "q2")],
        )
        other = next(r for r in self.store.list_signals() if r["product"] == "Q")
        other_before = get_signal(self.store, other["id"])
        self.update("При бронировании очереди ошибка 500.")
        old = get_signal(self.store, "P:bug:booking_queue")
        self.assertEqual(old["ticket_ids"], ["b", "c"])
        self.assertEqual(get_signal(self.store, other["id"]), other_before)
        self.assertEqual(self.store.signal_changes(other["id"]), [])
        self.update(ACTIVE, 3)
        restored = get_signal(self.store, old["id"])
        self.assertEqual(set(restored["ticket_ids"]), {"a", "b", "c"})
        source = next(s for s in restored["sources"] if s["ticket_id"] == "a")
        self.assertEqual(source["version"], 3)
        self.assertNotEqual(restored["revision"], old["revision"])

    def test_batch_conflict_rolls_back_versions_membership_and_events(self):
        before = self.seed()
        with self.assertRaises(ValueError):
            ingest_batch(
                self.store,
                [Ticket("a", "P", RESOLVED), Ticket("b", "P", "Новый текст")],
                ticket_versions={"a": 2, "b": 1},
            )
        self.assertEqual(get_signal(self.store, before["id"]), before)
        self.assertEqual([r["version"] for r in self.store.ticket_history("a")], [1])
        self.assertEqual(self.store.signal_changes(before["id"]), [])

    def test_same_content_higher_version_invalidates_stale_review_once(self):
        before = self.seed()
        self.update(ACTIVE)
        after = get_signal(self.store, before["id"])
        self.assertNotEqual(after["revision"], before["revision"])
        self.assertEqual(after["ticket_ids"], before["ticket_ids"])
        with self.assertRaises(ValueError):
            self.store.review(
                before["id"],
                "approved",
                "analyst",
                "Проверено",
                expected_revision=before["revision"],
            )
        self.assertEqual(len(self.store.signal_changes(before["id"])), 1)
        self.update(ACTIVE)
        self.assertEqual(get_signal(self.store, before["id"]), after)

    def test_versions_are_redacted_and_invalid_types_rejected(self):
        ingest_batch(self.store, [Ticket("a", "P", "Пишите alice@example.com")])
        self.update("Пишите bob@example.com или +7 999 123 45 67")
        history = json.dumps(self.store.ticket_history("a"), ensure_ascii=False)
        self.assertNotIn("alice@", history)
        self.assertNotIn("bob@", history)
        self.assertNotIn("999 123", history)
        for version in (True, 0, -1, 1.5, "3", 2**63):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.update("Другая версия", version)

    def test_legacy_database_is_migrated_once(self):
        path = Path(self.tmp.name) / "legacy.db"
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE staged_tickets(id TEXT PRIMARY KEY,product TEXT NOT NULL,text TEXT NOT NULL,channel TEXT NOT NULL,created_at TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO staged_tickets VALUES (?,?,?,?,?)",
                ("old", "P", ACTIVE, "web", "2026-11-01"),
            )
        for _ in range(2):
            store = SignalStore(path)
            try:
                self.assertEqual(
                    [r["version"] for r in store.ticket_history("old")], [1]
                )
                self.assertEqual(store.ticket_history("old")[0]["text"], ACTIVE)
            finally:
                store.close()

    def test_empty_signal_and_restoration_keep_identity(self):
        ingest_batch(self.store, [Ticket(i, "P", ACTIVE) for i in ("a", "b")])
        identity = "P:bug:booking_queue"
        ingest_batch(
            self.store,
            [Ticket(i, "P", RESOLVED) for i in ("a", "b")],
            ticket_versions={"a": 2, "b": 2},
        )
        empty = get_signal(self.store, identity)
        self.assertEqual(empty["ticket_count"], 0)
        self.assertEqual(empty["examples"], [])
        with self.assertRaises(ValueError):
            self.store.review(
                identity,
                "approved",
                "analyst",
                "Проверено",
                expected_revision=empty["revision"],
            )
        ingest_batch(
            self.store,
            [Ticket(i, "P", ACTIVE) for i in ("a", "b")],
            ticket_versions={"a": 3, "b": 3},
        )
        self.assertEqual(get_signal(self.store, identity)["ticket_count"], 2)
        self.assertEqual(len(self.store.list_signals()), 1)

    def test_new_symptom_can_create_new_signal_without_merging_old(self):
        self.seed()
        ingest_batch(
            self.store, [Ticket("x", "P", "При бронировании очереди ошибка 500.")]
        )
        self.update("При бронировании очереди ошибка 500.")
        groups = {
            frozenset(get_signal(self.store, s["id"])["ticket_ids"])
            for s in self.store.list_signals()
        }
        self.assertEqual(groups, {frozenset({"b", "c"}), frozenset({"a", "x"})})
        self.assertEqual(self.store.ticket_history("a")[-1]["version"], 2)

    def test_manual_exclusion_is_preserved_after_update(self):
        card = self.seed()
        self.store.exclude_source(
            card["id"],
            "a",
            "analyst",
            "Другая проблема",
            expected_revision=card["revision"],
        )
        before = get_signal(self.store, card["id"])
        self.update(ACTIVE + " Дополнительные сведения.")
        self.assertEqual(get_signal(self.store, card["id"]), before)
        self.assertEqual(self.store.signal_changes(card["id"]), [])

    def test_history_cannot_be_changed_even_through_sql(self):
        self.seed()
        for query in (
            "UPDATE ticket_versions SET text='changed' WHERE ticket_id='a'",
            "DELETE FROM ticket_versions WHERE ticket_id='a'",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute(query)
            self.store.db.rollback()
        self.assertEqual(self.store.ticket_history("a")[0]["text"], ACTIVE)

    def test_product_channel_and_original_date_are_immutable(self):
        ingest_batch(
            self.store,
            [Ticket("a", "P", ACTIVE)],
            ticket_dates={"a": "2026-11-01T10:00:00Z"},
        )
        for ticket, dates in [
            (Ticket("a", "Q", ACTIVE), {}),
            (Ticket("a", "P", ACTIVE, "email"), {}),
            (Ticket("a", "P", ACTIVE), {"a": "2026-11-02T10:00:00Z"}),
        ]:
            with self.assertRaises(ValueError):
                ingest_batch(
                    self.store, [ticket], ticket_versions={"a": 2}, ticket_dates=dates
                )
        self.assertEqual([r["version"] for r in self.store.ticket_history("a")], [1])

    def test_concurrent_updates_are_monotonic(self):
        from concurrent.futures import ThreadPoolExecutor

        self.seed()

        def update(version):
            store = SignalStore(self.path)
            try:
                try:
                    ingest_batch(
                        store,
                        [Ticket("a", "P", ACTIVE + f" Версия {version}.")],
                        ticket_versions={"a": version},
                    )
                    return "accepted"
                except ValueError:
                    return "stale"
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update, [2, 3]))
        self.assertIn("accepted", outcomes)
        versions = [r["version"] for r in self.store.ticket_history("a")]
        self.assertEqual(versions[-1], 3)
        self.assertEqual(len(versions), len(set(versions)))
        self.assertTrue(self.store.staged_tickets()[0].text.endswith("Версия 3."))

    def test_legacy_signal_cannot_change_meaning_on_full_context_replacement(self):
        from pulse import analyze

        old = [
            Ticket(i, "P", "При бронировании очереди ошибка 500.") for i in ("a", "b")
        ]
        self.store.stage_tickets(old)
        self.store.ingest(analyze(old)["signals"])
        identity = "P:bug:booking_queue"
        ingest_batch(
            self.store,
            [Ticket(i, "P", ACTIVE) for i in ("a", "b")],
            ticket_versions={"a": 2, "b": 2},
        )
        old_card = get_signal(self.store, identity)
        self.assertEqual(old_card["ticket_count"], 0)
        self.assertEqual(old_card["context"]["symptom"], "http_500")
        active = [r for r in self.store.list_signals() if r["ticket_count"]]
        self.assertEqual(len(active), 1)
        self.assertNotEqual(active[0]["id"], identity)
        (event,) = self.store.signal_changes(identity)
        self.assertEqual(event["removed_ticket_ids"], ["a", "b"])
        ingest_batch(self.store, old, ticket_versions={"a": 3, "b": 3})
        self.assertEqual(get_signal(self.store, identity)["ticket_count"], 2)
        self.assertEqual(len(self.store.list_signals()), 2)

    def test_semantic_empty_identity_does_not_capture_new_unrelated_cluster(self):
        class SameVector:
            def embed(self, texts):
                return [[1.0, 0.0] for _ in texts]

        options = {"mode": "semantic", "provider": SameVector()}
        ingest_batch(
            self.store, [Ticket(i, "P", ACTIVE) for i in ("a", "b")], **options
        )
        old = self.store.list_signals()[0]["id"]
        ingest_batch(
            self.store,
            [Ticket(i, "P", RESOLVED) for i in ("a", "b")],
            ticket_versions={"a": 2, "b": 2},
            **options,
        )
        ingest_batch(
            self.store, [Ticket(i, "P", ACTIVE) for i in ("e", "f")], **options
        )
        self.assertEqual(get_signal(self.store, old)["ticket_count"], 0)
        self.assertEqual(len(self.store.list_signals()), 2)

    def test_resolving_one_semantic_cluster_preserves_matching_sibling(self):
        class TwoVectors:
            def embed(self, texts):
                return [[0.0, 1.0] if "Группа два" in t else [1.0, 0.0] for t in texts]

        options = {"mode": "semantic", "provider": TwoVectors()}
        ingest_batch(
            self.store,
            [
                Ticket(i, "P", ACTIVE + (" Группа два." if i in ("c", "d") else ""))
                for i in ("a", "b", "c", "d")
            ],
            **options,
        )
        sibling = next(
            get_signal(self.store, r["id"])
            for r in self.store.list_signals()
            if r["id"].endswith(":c")
        )
        ingest_batch(
            self.store,
            [Ticket(i, "P", RESOLVED) for i in ("a", "b")],
            ticket_versions={"a": 2, "b": 2},
            **options,
        )
        self.assertEqual(get_signal(self.store, sibling["id"]), sibling)
        self.assertEqual(self.store.signal_changes(sibling["id"]), [])
