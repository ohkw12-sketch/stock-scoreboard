"""Offline storage/publication isolation checks; fixtures never touch live boards."""
import copy
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import ci_state
import record_publication as publication
import refresh_all
import refresh_store as store


class AtomicStoreTests(unittest.TestCase):
    def test_atomic_json_preserves_unicode_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.json"
            value = {"status": "검증완료", "value": 1.2}
            store.json_write(path, value)
            self.assertEqual(store.read_json(path), value)
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_invalid_nan_never_overwrites_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.json"
            store.json_write(path, {"verified": True})
            with self.assertRaises(ValueError):
                store.json_write(path, {"bad": float("nan")})
            self.assertEqual(store.read_json(path), {"verified": True})

    def test_failed_replace_keeps_previous_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.json"
            store.json_write(path, {"version": 1})
            with patch.object(store.os, "replace", side_effect=OSError("fixture failure")):
                with self.assertRaises(OSError):
                    store.json_write(path, {"version": 2})
            self.assertEqual(store.read_json(path), {"version": 1})
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_run_lock_is_released_after_builder_error(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                with store.run_lock(folder):
                    raise ValueError("fixture")
            self.assertFalse((Path(folder) / "refresh.lock").exists())
            with store.run_lock(folder):
                self.assertTrue((Path(folder) / "refresh.lock").exists())

    def test_public_fields_removes_nested_private_candidates(self):
        value = {"rows": [{"name": "테스트", "_raw": {"private": True}}], "_allRows": [1]}
        self.assertEqual(store.public_fields(value), {"rows": [{"name": "테스트"}]})
        self.assertIn("_allRows", value)

    def test_content_snapshot_keeps_original_stored_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.json"
            store.json_write(source, {"source": 1})
            first = store.snapshot_files(root / "snapshots", {"source": source},
                                         {"sourceCutoff": "2026-09-04", "firstStoredAt": "first"})
            second = store.snapshot_files(root / "snapshots", {"source": source},
                                          {"sourceCutoff": "2026-09-04", "firstStoredAt": "second"})
            manifest = store.read_json(root / "snapshots" / "manifests" / f"{first['snapshotId']}.json")
            self.assertEqual(first["snapshotId"], second["snapshotId"])
            self.assertEqual(manifest["firstStoredAt"], "first")
            self.assertEqual(len(list((root / "snapshots" / "objects").iterdir())), 1)


class SectionIsolationTests(unittest.TestCase):
    def setUp(self):
        self.previous = {"p2": {"rows": [{"ticker": "000001", "valueScore": 80}],
                                "refreshState": {"sourceCutoff": "2026-09-03", "generatedAt": "old"}},
                         "p1": {"rows": [{"ticker": "000002"}]}}
        self.context = {"generatedAt": "2026-09-07T18:00:00+09:00", "sourceCutoff": "2026-09-07",
                        "snapshotId": "new"}

    def test_builder_exception_keeps_values_and_original_source_date(self):
        states = {}
        before = copy.deepcopy(self.previous)
        def fail():
            raise RuntimeError("source unavailable")
        result = refresh_all.isolated_section("p2", fail, self.previous, states, self.context)
        self.assertEqual(result["rows"], before["p2"]["rows"])
        self.assertEqual(result["refreshState"]["sourceCutoff"], "2026-09-03")
        self.assertEqual(result["refreshState"]["generatedAt"], "old")
        self.assertEqual(states["p2"]["status"], "실패·이전유지")
        self.assertEqual(self.previous, before)

    def test_success_uses_own_generation_and_does_not_touch_other_section(self):
        states = {}
        before = copy.deepcopy(self.previous)
        result = refresh_all.isolated_section("p2", lambda: {"rows": [{"ticker": "000003"}]},
                                               self.previous, states, self.context)
        self.assertEqual(result["refreshState"]["snapshotId"], "new")
        self.assertEqual(states["p2"]["status"], "계산완료")
        self.assertEqual(self.previous, before)

    def test_explicit_source_failure_is_not_labeled_computed(self):
        states = {}
        result = refresh_all.isolated_section("p2", lambda: {
            "status": "가치 엔진 미갱신: 자료 없음", "rows": [],
            "dataStatus": {"status": "수집실패"}}, self.previous, states, self.context)
        self.assertEqual(states["p2"]["status"], "실패·이전유지")
        self.assertEqual(result["rows"], self.previous["p2"]["rows"])


class PublicationVerificationTests(unittest.TestCase):
    def setUp(self):
        self.board = {"meta": {"runId": "run-1"}, "p1": {"rows": [{"ticker": "000001"}]}}
        self.combined = {"runId": "run-1", "publicationState": "prepared", "rows": [{"ticker": "000001"}]}

    def test_exact_remote_pair_is_verified(self):
        self.assertTrue(publication.verified_generation(self.board, self.combined,
                                                        copy.deepcopy(self.board), copy.deepcopy(self.combined)))

    def test_same_run_id_with_changed_remote_rows_is_rejected(self):
        remote = copy.deepcopy(self.board)
        remote["p1"]["rows"][0]["ticker"] = "000002"
        self.assertFalse(publication.verified_generation(self.board, self.combined, remote, self.combined))

    def test_changed_remote_combination_is_rejected(self):
        remote = copy.deepcopy(self.combined)
        remote["rows"] = []
        self.assertFalse(publication.verified_generation(self.board, self.combined, self.board, remote))

    def test_mixed_generation_pair_is_rejected(self):
        remote = dict(self.combined, runId="run-2")
        self.assertFalse(publication.verified_generation(self.board, self.combined, self.board, remote))

    def test_missing_generation_is_not_publication_evidence(self):
        self.assertFalse(publication.verified_generation({"meta": {}}, {}, {"meta": {}}, {}))

    def test_intentionally_retained_combination_does_not_block_new_successful_sections(self):
        retained = dict(self.combined, runId='previous-run')
        self.assertTrue(publication.verified_generation(self.board, retained, self.board, retained))


class CiStateIsolationTests(unittest.TestCase):
    def valid_state(self, root, quality='정상'):
        staging = root/'inputs'
        staging.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{'ticker': '000001', 'date': pd.Timestamp('2026-09-04'), 'close': 100}]).to_pickle(
            staging/'prices.pkl.gz', compression='gzip')
        pd.DataFrame([{'ticker': '000001', 'op_current': 10}]).to_pickle(
            staging/'fundamentals.pkl.gz', compression='gzip')
        store.json_write(staging/'report.json', {'qualityStatus': quality, 'latestPriceDate': '2026-09-04'})
        manifest = store.snapshot_files(root/'cache/snapshots', {
            'prices': staging/'prices.pkl.gz', 'fundamentals': staging/'fundamentals.pkl.gz',
            'report': staging/'report.json'}, {'sourceCutoff': '2026-09-04', 'firstStoredAt': 'first'})
        store.json_write(root/'test_output/verified_snapshot.json', {'snapshotId': manifest['snapshotId']})
        return manifest

    def test_known_nonsecret_cache_files_are_allowed(self):
        for path in ("cache/kis_consensus_history.csv", "cache/dart_disclosure_state.json",
                     "cache/dart_periods/2026_11012.json", "test_output/verified_prices.pkl"):
            self.assertTrue(ci_state.allowed(path), path)

    def test_credentials_and_tracked_user_holdings_are_not_packaged(self):
        for path in (".env", "cache/kis_token.json", "cache/credentials.json", "data.json",
                     "youtube-market.json", "cache/refresh.lock"):
            self.assertFalse(ci_state.allowed(path), path)

    def test_parent_segments_cannot_bypass_artifact_whitelist(self):
        self.assertFalse(ci_state.allowed("cache/dart_periods/../../rotation_screener.py"))
        self.assertFalse(ci_state.allowed("cache/growth/documents/../../../.env"))

    def test_absolute_drive_unc_ads_and_nested_secret_paths_are_denied(self):
        for path in ('/cache/dart_periods/2026_11012.json', 'C:/cache/dart_periods/2026_11012.json',
                     '//server/cache/dart_periods/2026_11012.json', 'cache/kis_consensus_state.json:secret',
                     'cache/dart_periods/.env', 'cache/growth/documents/keys.json'):
            self.assertFalse(ci_state.allowed(path), path)

    def test_actual_incremental_public_growth_ledgers_are_allowed(self):
        for path in ('cache/growth/news_queue.json', 'cache/growth/news_search_state.json',
                     'cache/growth/trade_release_cache.json'):
            self.assertTrue(ci_state.allowed(path), path)

    def test_provided_documents_and_captions_are_local_only(self):
        for path in ('cache/growth/verified_document_ledger.json', 'cache/growth/verified_document_status.json',
                     'cache/growth/verified_documents_input.json', 'cache/youtube/content_ledger.json',
                     'cache/youtube/content_status.json', 'cache/youtube/verified_transcripts_input.json'):
            self.assertFalse(ci_state.allowed(path), path)

    def test_restore_rejects_traversal_even_when_target_is_within_repo(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "original.json"
            store.json_write(original, {"version": "original"})
            archive_path = root / "unsafe.tar.gz"
            payload = b'{"version":"unexpected"}'
            with tarfile.open(archive_path, "w:gz") as archive:
                member = tarfile.TarInfo("cache/dart_periods/../../original.json")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            with patch.object(ci_state, "ROOT", root), patch("sys.argv", ["ci_state.py", "restore", "--archive", str(archive_path)]):
                with self.assertRaises(RuntimeError):
                    ci_state.main()
            self.assertEqual(store.read_json(original), {"version": "original"})

    def test_archive_roundtrip_does_not_include_nonwhitelisted_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.valid_state(root)
            store.json_write(root / "cache" / "kis_consensus_state.json", {"cursor": 3})
            store.json_write(root / "cache" / "credentials.json", {"dummy_secret": "fixture-only"})
            archive_path = root / "state.tar.gz"
            with patch.object(ci_state, "ROOT", root), patch("sys.argv", ["ci_state.py", "pack", "--archive", str(archive_path)]):
                ci_state.main()
            with tarfile.open(archive_path, "r:gz") as archive:
                names = archive.getnames()
            self.assertIn("cache/kis_consensus_state.json", names)
            self.assertNotIn("cache/credentials.json", names)

    def test_empty_or_failed_verified_set_cannot_replace_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root/'state.tar.gz'
            archive.write_bytes(b'previous-good-archive')
            with self.assertRaises(RuntimeError):
                ci_state.pack(root, archive)
            self.assertEqual(archive.read_bytes(), b'previous-good-archive')
            self.valid_state(root, quality='부분실패')
            with self.assertRaises(RuntimeError):
                ci_state.pack(root, archive)
            self.assertEqual(archive.read_bytes(), b'previous-good-archive')

    def test_primary_snapshot_restores_without_legacy_pickle_aliases(self):
        with tempfile.TemporaryDirectory() as source_folder, tempfile.TemporaryDirectory() as target_folder:
            source, target = Path(source_folder), Path(target_folder)
            primary = self.valid_state(source)
            archive = source/'state.tar.gz'
            saved = ci_state.pack(source, archive)
            restored = ci_state.restore(target, archive)
            self.assertEqual(saved['snapshotId'], restored['snapshotId'])
            self.assertEqual(store.read_json(target/'test_output/verified_snapshot.json')['snapshotId'], primary['snapshotId'])
            self.assertFalse((target/'test_output/verified_prices.pkl').exists())
            self.assertTrue((target/'cache/snapshots/manifests'/f"{primary['snapshotId']}.json").exists())

    def test_old_evidence_snapshot_objects_and_manifests_survive_restore(self):
        with tempfile.TemporaryDirectory() as source_folder, tempfile.TemporaryDirectory() as target_folder:
            source, target = Path(source_folder), Path(target_folder)
            self.valid_state(source)
            evidence_path = source/'inputs/evidence.json'
            store.json_write(evidence_path, {'events': [{'publishedAt': '2026-01-01'}]})
            old = store.snapshot_files(source/'cache/snapshots', {'evidence': evidence_path},
                                       {'sourceCutoff': '2026-08-01', 'firstStoredAt': 'old'})
            archive = source/'state.tar.gz'
            ci_state.pack(source, archive)
            ci_state.restore(target, archive)
            manifest_path = target/'cache/snapshots/manifests'/f"{old['snapshotId']}.json"
            self.assertEqual(store.read_json(manifest_path)['firstStoredAt'], 'old')
            object_path = target/'cache/snapshots'/old['files']['evidence']['path'].replace('\\', '/')
            self.assertEqual(store.read_json(object_path)['events'][0]['publishedAt'], '2026-01-01')

    def test_tampered_bytes_rejected_before_touching_destination(self):
        with tempfile.TemporaryDirectory() as source_folder, tempfile.TemporaryDirectory() as target_folder:
            source, target = Path(source_folder), Path(target_folder)
            self.valid_state(source)
            archive = source/'good.tar.gz'
            ci_state.pack(source, archive)
            damaged = source/'damaged.tar.gz'
            with tarfile.open(archive, 'r:gz') as original, tarfile.open(damaged, 'w:gz') as output:
                for member in original.getmembers():
                    raw = original.extractfile(member).read()
                    if member.name == 'test_output/verified_snapshot.json':
                        raw = b'{"snapshotId":"unexpected"}'
                    changed = copy.copy(member)
                    changed.size = len(raw)
                    output.addfile(changed, io.BytesIO(raw))
            store.json_write(target/'test_output/verified_snapshot.json', {'snapshotId': 'original'})
            with self.assertRaises(RuntimeError):
                ci_state.restore(target, damaged)
            self.assertEqual(store.read_json(target/'test_output/verified_snapshot.json')['snapshotId'], 'original')

    def test_missing_object_is_not_packaged_as_valid_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = self.valid_state(root)
            missing = root/'cache/snapshots'/manifest['files']['prices']['path'].replace('\\', '/')
            missing.unlink()
            with self.assertRaises(RuntimeError):
                ci_state.pack(root, root/'state.tar.gz')

    def test_known_secret_value_in_allowed_report_blocks_pack(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.valid_state(root)
            store.json_write(root/'cache/kis_consensus_state.json', {'problem': 'fixture-secret-do-not-upload'})
            with patch.dict('os.environ', {'KIS_APP_SECRET': 'fixture-secret-do-not-upload'}):
                with self.assertRaises(RuntimeError):
                    ci_state.pack(root, root/'state.tar.gz')
            self.assertFalse((root/'state.tar.gz').exists())

    def test_provided_excerpt_cannot_leak_through_snapshot_objects(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.valid_state(root)
            provided = root/'inputs/provided-evidence.json'
            store.json_write(provided, {'events': [{'receipt': 'DOC-fixture', 'excerpt': 'private fixture text'}]})
            private = store.snapshot_files(root/'cache/snapshots', {'evidence': provided},
                                           {'sourceCutoff': '2026-09-04', 'firstStoredAt': 'local-only'})
            manifest = ci_state.pack(root, root/'state.tar.gz')
            self.assertEqual(manifest['privateSnapshotsExcluded'], 1)
            self.assertNotIn(f"cache/snapshots/manifests/{private['snapshotId']}.json", manifest['files'])
            object_name = 'cache/snapshots/' + private['files']['evidence']['path'].replace('\\', '/')
            self.assertNotIn(object_name, manifest['files'])
            self.assertTrue((root/'cache/snapshots'/private['files']['evidence']['path']).exists())


if __name__ == "__main__":
    unittest.main()
