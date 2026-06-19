import tempfile
import unittest
from pathlib import Path

from pipeline.utils import load_config


class LoadConfigTest(unittest.TestCase):
    def test_config_local_overrides_public_config_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.yaml").write_text(
                """
openrouter:
  enabled: false
  api_key: ""
  base_url: https://api.openrouter.ai
music:
  backend: ace_step
""".strip(),
                encoding="utf-8",
            )
            (root / "config.local.yaml").write_text(
                """
openrouter:
  enabled: true
  api_key: local-only
""".strip(),
                encoding="utf-8",
            )

            cfg = load_config(str(root / "config.yaml"))

        self.assertTrue(cfg["openrouter"]["enabled"])
        self.assertEqual(cfg["openrouter"]["api_key"], "local-only")
        self.assertEqual(cfg["openrouter"]["base_url"], "https://api.openrouter.ai")
        self.assertEqual(cfg["music"]["backend"], "ace_step")


if __name__ == "__main__":
    unittest.main()
