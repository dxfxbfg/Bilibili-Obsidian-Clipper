import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import build_release


class ReleaseTests(unittest.TestCase):
    def test_browser_variants_exclude_local_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extension = root / "extension"
            extension.mkdir()
            (extension / "ai").mkdir()
            (extension / "ai" / "sse.js").write_text("// shared parser")
            (extension / "background.js").write_text("// background")
            (extension / "whisper-helper.js").write_text("// helper controller")
            (extension / "targets.local.json").write_text('{"apiKey":"must-not-ship"}')
            (extension / ".env.local").write_text("SECRET=must-not-ship")
            (extension / "ai" / ".env").write_text("NESTED=must-not-ship")
            release = root / "release"
            release.mkdir()
            manifest = build_release.load_manifest()
            original = json.dumps(manifest)
            with patch.object(build_release, "EXTENSION_DIR", extension), patch.object(build_release, "RELEASE_DIR", release):
                for browser in ("chrome", "firefox"):
                    with self.subTest(browser=browser):
                        _, archive = build_release.build_variant(manifest, browser, "test")
                        with zipfile.ZipFile(archive) as z:
                            self.assertFalse(any(".env" in n or "targets.local.json" in n for n in z.namelist()))
                            self.assertIn("ai/sse.js", z.namelist())
                            self.assertNotIn(b"must-not-ship", b"".join(z.read(n) for n in z.namelist()))
                            built = json.loads(z.read("manifest.json"))
                        if browser == "chrome":
                            self.assertEqual(built["background"]["service_worker"], "background.js")
                            self.assertNotIn("browser_specific_settings", built)
                        else:
                            self.assertEqual(built["background"]["scripts"], ["ai/sse.js", "whisper-helper.js", "background.js"])
                            self.assertNotIn("sidePanel", built["permissions"])
                            self.assertNotIn("service_worker", built["background"])
            self.assertEqual(json.dumps(manifest), original)


if __name__ == "__main__":
    unittest.main()
