import tempfile,unittest
from pathlib import Path
from alignment_demo import fixture,run
from literary.tools import alignment as a,alignment_release as releases,alignment_v2 as v2

class PublicAlignmentTests(unittest.TestCase):
    def test_release_roundtrip_is_not_review(self):
        with tempfile.TemporaryDirectory() as d:
            result=run(Path(d));self.assertEqual(result['reviewed_chapters'],0)
    def test_source_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            fixture(Path(d));(Path(d)/'source.txt').write_text('changed',encoding='utf-8')
            with self.assertRaisesRegex(a.AlignmentError,'SHA-256 mismatch'):a.load_context(Path(d))
    def test_object_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            c=fixture(Path(d));v2.checkpoint_legacy(c);s=releases.Store(c.alignment_root)
            digest=s.put_object(b'original');s._path('objects',digest).write_bytes(b'tampered')
            with self.assertRaisesRegex(a.AlignmentError,'hash mismatch'):s.get_object(digest)
    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(a.AlignmentError):a._repo_path(Path(d),'../outside')

if __name__=='__main__':unittest.main()
