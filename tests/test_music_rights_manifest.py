import unittest
from pathlib import Path

from pipeline.render import _music_rights_info


class MusicRightsManifestTest(unittest.TestCase):
    def test_folder_music_is_user_library_not_ai(self) -> None:
        info, disclosure, reason = _music_rights_info(
            Path("music/chill/track.mp3"),
            None,
            "chill",
            {"music": {"backend": "ace_step"}},
        )

        self.assertEqual(info["source_type"], "user_library")
        self.assertEqual(info["license_status"], "user_provided_unverified")
        self.assertFalse(disclosure)
        self.assertIn("local music folder", reason)

    def test_chosen_generated_music_is_marked_ai(self) -> None:
        chosen = Path("output/job/music_variants/variant_1.wav")

        info, disclosure, reason = _music_rights_info(
            chosen,
            chosen,
            "dramatic",
            {"music": {"backend": "ace_step"}},
        )

        self.assertEqual(info["source_type"], "ai_generated")
        self.assertEqual(info["backend"], "ace_step")
        self.assertTrue(disclosure)
        self.assertIn("generated", reason)

    def test_no_music_is_explicitly_recorded(self) -> None:
        info, disclosure, reason = _music_rights_info(
            None,
            None,
            "comedy",
            {"music": {"backend": "ace_step"}},
        )

        self.assertEqual(info["source_type"], "none")
        self.assertEqual(info["license_status"], "no_background_music")
        self.assertFalse(disclosure)
        self.assertIn("not added", reason)


if __name__ == "__main__":
    unittest.main()
