from django.test import TestCase, Client
class HealthTest(TestCase):
    def setUp(self):
        self.client = Client()
    def test_health_check(self):
        res = self.client.get('/healthz')
        self.assertEqual(res.status_code, 200)
    def test_api_health_check(self):
        res = self.client.get('/api/health/')
        self.assertEqual(res.status_code, 200)
