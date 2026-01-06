import unittest

import middleware


class DummyResp:
    def __init__(self, status_code, data=None, text=''):
        self.status_code = status_code
        self._data = data or {}
        self.text = text

    def json(self):
        return self._data


class TestUpdateSegmentAttributeRateLimit(unittest.TestCase):
    def setUp(self):
        # Override the proxy bearer token
        middleware.API_BEARER_TOKEN = 'testtoken'
        # disable actual sleep in rate-limit loops
        middleware.time.sleep = lambda *_: None
        self.client = middleware.app.test_client()

    def test_rate_limit_retry(self):
        # Simulate GET first returning 429, then contacts page, then empty
        get_seq = [
            DummyResp(429, text='Rate limit'),
            DummyResp(200, data={'contacts': [{'id': '1'}]}),
            DummyResp(200, data={'contacts': []}),
        ]
        idx = {'i': 0}
        def fake_get(*args, **kwargs):
            resp = get_seq[idx['i']]
            idx['i'] += 1
            return resp
        middleware.BREVO_SESSION.get = fake_get

        # Simulate POST to import endpoint failing twice then succeeding
        post_seq = [
            DummyResp(429, text='Rate limit'),
            DummyResp(429, text='Rate limit'),
            DummyResp(200, data={}),
        ]
        idx2 = {'i': 0}
        def fake_post(*args, **kwargs):
            resp = post_seq[idx2['i']]
            idx2['i'] += 1
            return resp
        middleware.BREVO_SESSION.post = fake_post

        rv = self.client.post(
            '/update_segment_attribute',
            headers={'Authorization': 'Bearer testtoken'},
            json={
                'brevo_api_key': 'key',
                'segment_id': 123,
                'attribute_name': 'FOO',
                'attribute_value': 'BAR'
            }
        )
        self.assertEqual(rv.status_code, 200)
        data = rv.get_json()
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['updated'], 1)
        self.assertEqual(data['failures'], [])


if __name__ == '__main__':
    unittest.main()
