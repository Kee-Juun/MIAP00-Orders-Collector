from pathlib import Path
import unittest


class PackagingTests(unittest.TestCase):
    def test_onefile_runtime_directory_is_resolved_for_the_launching_user(self):
        project_root = Path(__file__).resolve().parents[1]
        spec_text = (project_root / "MIAP00 Orders Collector.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn("runtime_tmpdir=None", spec_text)
        self.assertNotIn("MIAP00Runtime", spec_text)
        self.assertNotIn("os.environ.get('LOCALAPPDATA'", spec_text)


if __name__ == "__main__":
    unittest.main()
