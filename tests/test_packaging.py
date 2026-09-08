from pathlib import Path
import unittest

from utils.packaging import filter_portable_binaries, is_nonportable_binary


class PackagingTests(unittest.TestCase):
    def test_onefile_runtime_directory_is_resolved_for_the_launching_user(self):
        project_root = Path(__file__).resolve().parents[1]
        spec_text = (project_root / "MIAP00 Orders Collector.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn("runtime_tmpdir=None", spec_text)
        self.assertNotIn("MIAP00Runtime", spec_text)
        self.assertNotIn("os.environ.get('LOCALAPPDATA'", spec_text)

    def test_windows_api_forwarders_and_system_ucrt_are_not_bundled(self):
        self.assertTrue(
            is_nonportable_binary(
                "api-ms-win-crt-runtime-l1-1-0.dll",
                r"C:\foreign\bin\api-ms-win-crt-runtime-l1-1-0.dll",
            )
        )
        self.assertTrue(
            is_nonportable_binary(
                "ucrtbase.dll",
                r"C:\foreign\bin\ucrtbase.dll",
            )
        )

    def test_private_codex_runtime_dependencies_are_not_bundled(self):
        self.assertTrue(
            is_nonportable_binary(
                "icuuc.dll",
                r"C:\Users\builder\.cache\codex-runtimes\runtime\icuuc.dll",
            )
        )

    def test_pyqt_and_microsoft_runtime_dependencies_are_preserved(self):
        entries = [
            (
                r"PyQt6\Qt6\bin\Qt6Core.dll",
                r"C:\Python\site-packages\PyQt6\Qt6\bin\Qt6Core.dll",
                "BINARY",
            ),
            (
                r"PyQt6\Qt6\bin\VCRUNTIME140.dll",
                r"C:\Python\site-packages\PyQt6\Qt6\bin\VCRUNTIME140.dll",
                "BINARY",
            ),
            (
                "api-ms-win-core-file-l1-1-0.dll",
                r"C:\foreign\api-ms-win-core-file-l1-1-0.dll",
                "BINARY",
            ),
        ]

        self.assertEqual(filter_portable_binaries(entries), entries[:2])


if __name__ == "__main__":
    unittest.main()
