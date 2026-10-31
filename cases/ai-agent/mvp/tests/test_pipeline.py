import unittest
from pulse import Ticket
from pipeline import analyze_semantic

class FakeProvider:
    def __init__(self, vectors):
        self.vectors = vectors
        self.seen = []
    def embed(self, texts):
        self.seen = texts
        return self.vectors

class PipelineTests(unittest.TestCase):
    def test_positive_cluster(self):
        t=[Ticket('1','P','Очередь ошибка 404'),Ticket('2','P','Бронирование очереди ошибка 404')]
        r=analyze_semantic(t, FakeProvider([[1,0],[1,0]]))
        self.assertEqual(r['signals'][0]['ticket_count'],2)
    def test_no_cross_product(self):
        t=[Ticket('1','P','Очередь ошибка 404'),Ticket('2','Q','Очередь ошибка 404')]
        self.assertEqual(analyze_semantic(t,FakeProvider([[1,0],[1,0]]))['signals'],[])
    def test_no_semantic_match(self):
        t=[Ticket('1','P','Очередь ошибка 404'),Ticket('2','P','Очередь ошибка 404')]
        self.assertEqual(analyze_semantic(t,FakeProvider([[1,0],[0,1]]))['signals'],[])
    def test_unknown(self):
        self.assertEqual(analyze_semantic([Ticket('1','P','Привет')],FakeProvider([[1,0]]))['unclassified'],1)
    def test_wrong_count(self):
        with self.assertRaises(ValueError):
            analyze_semantic([Ticket('1','P','Очередь ошибка 404')],FakeProvider([]))
    def test_duplicate_ids(self):
        with self.assertRaises(ValueError):
            analyze_semantic([Ticket('1','P','x'),Ticket('1','P','y')],FakeProvider([]))
    def test_redaction_before_embedding(self):
        p=FakeProvider([[1,0]])
        analyze_semantic([Ticket('1','P','Очередь ошибка 404 +7 999 123 45 67')],p)
        self.assertNotIn('999',p.seen[0])
    def test_empty(self):
        self.assertEqual(analyze_semantic([],FakeProvider([]))['signals'],[])
    def test_invalid_threshold(self):
        with self.assertRaises(ValueError):
            analyze_semantic([],FakeProvider([]),threshold=1.2)

if __name__=='__main__':
    unittest.main()
