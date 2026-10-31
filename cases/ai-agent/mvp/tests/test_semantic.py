import unittest
from semantic import cosine, match_pair, evaluate_pairs

class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.a={'id':'1','product':'A','category':'bug','workflow':'booking'}
        self.b={'id':'2','product':'A','category':'bug','workflow':'booking'}
    def test_identical_embeddings(self):
        self.assertAlmostEqual(cosine([1,2],[1,2]),1)
    def test_invalid_embedding(self):
        with self.assertRaises(ValueError): cosine([0,0],[1,2])
    def test_matching(self):
        self.assertTrue(match_pair(self.a,self.b,([1,0],[1,0])).accepted)
    def test_product_boundary(self):
        self.b['product']='B'
        self.assertEqual(match_pair(self.a,self.b,([1,0],[1,0])).reason,'different_product')
    def test_workflow_boundary(self):
        self.b['workflow']='login'
        self.assertFalse(match_pair(self.a,self.b,([1,0],[1,0])).accepted)
    def test_category_boundary(self):
        self.b['category']='feature_request'
        self.assertFalse(match_pair(self.a,self.b,([1,0],[1,0])).accepted)
    def test_below_threshold(self):
        self.assertFalse(match_pair(self.a,self.b,([1,0],[0,1])).accepted)
    def test_metrics(self):
        result=evaluate_pairs([True,True,False],[True,False,True])
        self.assertEqual((result['tp'],result['fp'],result['fn']),(1,1,1))
    def test_invalid_gold(self):
        with self.assertRaises(ValueError): evaluate_pairs([],[])

if __name__=='__main__': unittest.main()
