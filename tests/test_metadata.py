"""metadata.yaml must parse: AstrBot falls back to default metadata on a YAML error
(a desc with an unquoted ': ' did that until Nov 8)."""

import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # AstrBot ships PyYAML; a bare checkout may not
    yaml = None


@unittest.skipIf(yaml is None, "PyYAML not installed")
class MetadataTests(unittest.TestCase):
    def test_parses_with_required_fields(self):
        meta = yaml.safe_load(Path("metadata.yaml").read_text(encoding="utf-8"))
        self.assertIsInstance(meta, dict)
        for k in ("name", "author", "desc", "version", "repo"):
            self.assertIsInstance(meta.get(k), str, k)
        self.assertEqual(meta["name"], "astrbot_plugin_regular")


if __name__ == "__main__":
    unittest.main()
