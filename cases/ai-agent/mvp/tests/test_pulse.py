import unittest
from pulse import Ticket, analyze, redact

class PulseTests(unittest.TestCase):
    def test_repeated_problem(self):
        result = analyze([Ticket('1', 'P', 'Очередь: ошибка 404'), Ticket('2', 'P', 'Бронирование очереди ошибка 404')])
        self.assertEqual(len(result['signals']), 1)
        self.assertEqual(result['signals'][0]['ticket_count'], 2)

    def test_product_isolation(self):
        result = analyze([Ticket('1', 'P1', 'Очередь ошибка 404'), Ticket('2', 'P2', 'Очередь ошибка 404')])
        self.assertEqual(result['signals'], [])

    def test_unknown_not_published(self):
        self.assertEqual(analyze([Ticket('1','P','Привет')])['signals'], [])

    def test_dedup_id(self):
        tickets = [Ticket('1','P','Очередь ошибка 404')]*2
        self.assertEqual(analyze(tickets)['signals'], [])

    def test_phone_redaction(self):
        self.assertNotIn('999', redact('Позвоните +7 999 123 45 67'))

if __name__ == '__main__':
    unittest.main()
