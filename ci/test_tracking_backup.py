import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / 'bootstrap/nebius/management/tracking/backup-artifacts.py'
SPEC = importlib.util.spec_from_file_location('tracking_backup', MODULE)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class Store:
    def __init__(self):
        self.objects = {'source': {}, 'target': {}}
        self.markers = {'source': [], 'target': []}
        self.uploads = {'source': [], 'target': []}
        self.puts = []
        self.reads = []

    def get_bucket_versioning(self, *, Bucket):
        return {'Status': 'Enabled'}

    def get_paginator(self, operation):
        class Pager:
            def paginate(_, *, Bucket, Prefix=""):
                if operation == 'list_multipart_uploads':
                    return [{'Uploads': self.uploads[Bucket]}]
                return [{
                    'Versions': [dict(Key=key, VersionId=version, Size=len(value), IsLatest=True)
                                 for key, (version, value) in self.objects[Bucket].items() if key.startswith(Prefix)],
                    'DeleteMarkers': self.markers[Bucket],
                }]
        return Pager()

    def get_object(self, *, Bucket, Key, VersionId):
        self.reads.append((Bucket, Key, VersionId))
        version, value = self.objects[Bucket][Key]
        if version != VersionId:
            raise AssertionError('Version was not pinned')
        return {'VersionId': version, 'ContentLength': len(value), 'Body': io.BytesIO(value)}

    def put_object(self, *, Bucket, Key, Body, ContentLength, IfNoneMatch):
        if Key in self.objects[Bucket] or IfNoneMatch != '*':
            raise AssertionError('Object overwrite attempted')
        value = Body.read()
        assert len(value) == ContentLength
        self.puts.append((Bucket, Key))
        self.objects[Bucket][Key] = ('restored', value)
        return {'VersionId': 'restored'}


class TrackingBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / 'snapshot'
        self.store = Store()
        self.store.objects['source'] = {'artifacts/1/one': ('v1', b'report'), 'elsewhere/private': ('v2', b'other')}

    def snapshot(self, max_bytes=20):
        return backup.snapshot(self.store, 'source', 'artifacts/', self.destination, max_bytes, 4)

    def restore(self):
        return backup.restore(self.store, 'target', 'artifacts/', self.destination, 20, 4)

    def test_snapshot_and_exact_verified_restore(self):
        self.snapshot()
        manifest = json.loads((self.destination / 'manifest.json').read_text())
        self.assertEqual([row['version_id'] for row in manifest['artifacts']], ['v1'])
        self.assertEqual(manifest['artifacts'][0]['size'], 6)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.destination / 'manifest.json').stat().st_mode & 0o777, 0o600)
        self.restore()
        self.assertEqual(self.store.reads, [('source', 'artifacts/1/one', 'v1'), ('target', 'artifacts/1/one', 'restored')])
        self.assertEqual(self.store.objects['target']['artifacts/1/one'][1], b'report')

    def test_max_bytes_checked_before_download(self):
        with self.assertRaises(ValueError):
            self.snapshot(max_bytes=5)
        self.assertEqual(self.store.reads, [])
        self.assertFalse((self.destination / 'manifest.json').exists())

    def test_corrupt_content_rejected_before_first_put(self):
        self.snapshot()
        (self.destination / 'objects' / '0').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual(self.store.puts, [])

    def test_nonempty_target_or_pending_multipart_rejected(self):
        self.snapshot()
        for collection in (self.store.objects, self.store.markers, self.store.uploads):
            with self.subTest(collection=collection):
                original = collection['target']
                collection['target'] = {'existing': ('old', b'bytes')} if isinstance(original, dict) else [{'Key': 'existing', 'VersionId': 'old'}]
                with self.assertRaises(ValueError):
                    self.restore()
                self.assertEqual(self.store.puts, [])
                collection['target'] = original

    def test_source_bucket_cannot_be_restore_target(self):
        self.snapshot()
        with self.assertRaises(ValueError):
            backup.restore(self.store, 'source', 'artifacts/', self.destination, 20, 4)
        self.assertEqual(self.store.puts, [])

    def test_concurrent_source_change_has_no_completed_manifest(self):
        read = self.store.get_object

        def changed(**args):
            result = read(**args)
            self.store.objects['source']['artifacts/1/one'] = ('v2', b'changed')
            return result

        self.store.get_object = changed
        with self.assertRaises(ValueError):
            self.snapshot()
        self.assertFalse((self.destination / 'manifest.json').exists())


if __name__ == '__main__':
    unittest.main()
