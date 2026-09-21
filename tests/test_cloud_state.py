import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import cloud_state
from refresh_store import store_verified_frames, load_verified_frames


class CloudStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'source'
        self.state = Path(self.temp.name)/'remote'
        self.target = Path(self.temp.name)/'restored'
        self.key = os.urandom(32)
        self.frames = pd.DataFrame([{'ticker': '000001', 'close': 100}])
        store_verified_frames(self.root/'test_output', self.root/'cache', self.frames,
                              self.frames, {'qualityStatus': '정상', 'latestPriceDate': '2026-09-18'}, 'now')

    def test_roundtrip_including_private_evidence_but_excluding_cookies_and_secrets(self):
        evidence = self.root/'cache/youtube/content_ledger.json'
        evidence.parent.mkdir()
        evidence.write_text(json.dumps([{'transcript': 'private source text'}]))
        (self.root/'cache/credentials.json').write_text('never upload')
        cloud_state.save(self.root, self.state, self.key)
        self.assertFalse(any(b'private source text' in p.read_bytes() for p in self.state.rglob('*') if p.is_file()))
        cloud_state.restore(self.target, self.state, self.key)
        self.assertEqual(evidence.read_bytes(), (self.target/'cache/youtube/content_ledger.json').read_bytes())
        self.assertFalse((self.target/'cache/credentials.json').exists())
        prices, _, _ = load_verified_frames(self.target/'test_output', self.target/'cache')
        pd.testing.assert_frame_equal(prices, self.frames)

    def test_tampering_or_wrong_key_does_not_replace_existing_state(self):
        cloud_state.save(self.root, self.state, self.key)
        cloud_state.restore(self.target, self.state, self.key)
        pointer = self.target/'test_output/verified_snapshot.json'
        old = pointer.read_bytes()
        with self.assertRaises(Exception):
            cloud_state.restore(self.target, self.state, os.urandom(32))
        self.assertEqual(pointer.read_bytes(), old)
        obj = next((self.state/'objects').glob('*.bin'))
        obj.write_bytes(b'corrupt')
        with self.assertRaises(Exception):
            cloud_state.restore(self.target, self.state, self.key)
        self.assertEqual(pointer.read_bytes(), old)

    def test_unchanged_large_files_reuse_objects_and_split_below_git_limit(self):
        path = self.root/'cache/krx_prices.csv.gz'
        path.write_bytes(b'a' * 200)
        with patch.object(cloud_state, 'CHUNK', 64):
            cloud_state.save(self.root, self.state, self.key)
            before = {p.name: p.read_bytes() for p in (self.state/'objects').glob('*.bin')}
            cloud_state.save(self.root, self.state, self.key)
        for name, data in before.items():
            self.assertEqual(data, (self.state/'objects'/name).read_bytes())
        cloud_state.restore(self.target, self.state, self.key)
        self.assertEqual(path.read_bytes(), (self.target/'cache/krx_prices.csv.gz').read_bytes())

    def test_path_escape_is_rejected_before_live_writes(self):
        cloud_state.save(self.root, self.state, self.key)
        pointer = self.state/'state.enc'
        payload = json.loads(cloud_state.unseal(pointer.read_bytes(), self.key, 'manifest'))
        payload['files']['../outside.json'] = next(iter(payload['files'].values()))
        pointer.write_bytes(cloud_state.seal(json.dumps(payload).encode(), self.key, 'manifest'))
        with self.assertRaises(ValueError):
            cloud_state.restore(self.target, self.state, self.key)
        self.assertFalse((self.target/'test_output/verified_snapshot.json').exists())


if __name__ == '__main__':
    unittest.main()
