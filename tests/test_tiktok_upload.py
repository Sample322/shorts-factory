import unittest

from pipeline.tiktok_upload import (
    MAX_CHUNK_SIZE,
    MIN_CHUNK_SIZE,
    adapt_metadata_for_tiktok,
    extract_authorization_code,
    make_upload_plan,
)
from pipeline.youtube_upload import VideoMetadata


class TikTokUploadHelpersTest(unittest.TestCase):
    def test_adapt_metadata_removes_youtube_only_and_spam_hashtags(self) -> None:
        youtube_meta = VideoMetadata(
            title="ЕМУ ТЯЖЕЛО ПОВЕРИТЬ В ЭТО",
            description=(
                "Ему тяжело поверить в это... #сериал #youtubeshorts "
                "#fyp #мистика"
            ),
            tags=["русские сериалы", "youtube shorts", "кино"],
        )

        tiktok_meta = adapt_metadata_for_tiktok(
            youtube_meta,
            {"tiktok": {"max_caption_hashtags": 8}},
        )

        caption_lower = tiktok_meta.caption.lower()
        self.assertIn("Ему тяжело поверить", tiktok_meta.caption)
        self.assertIn("#сериал", caption_lower)
        self.assertIn("#мистика", caption_lower)
        self.assertIn("#tiktok", caption_lower)
        self.assertNotIn("#youtubeshorts", caption_lower)
        self.assertNotIn("#fyp", caption_lower)

    def test_extract_authorization_code_accepts_full_redirect_url(self) -> None:
        code = extract_authorization_code(
            "https://example.com/tiktok/callback?code=abc%2F123&state=xyz"
        )

        self.assertEqual(code, "abc/123")

    def test_make_upload_plan_keeps_large_chunks_within_tiktok_limits(self) -> None:
        small_size = 4 * 1024 * 1024
        self.assertEqual(make_upload_plan(small_size), (small_size, 1))

        large_size = 65 * 1024 * 1024
        chunk_size, chunk_count = make_upload_plan(large_size)
        final_chunk_size = large_size - chunk_size * (chunk_count - 1)

        self.assertGreater(chunk_count, 1)
        self.assertGreaterEqual(chunk_size, MIN_CHUNK_SIZE)
        self.assertLessEqual(chunk_size, MAX_CHUNK_SIZE)
        self.assertGreater(final_chunk_size, 0)
        self.assertLessEqual(final_chunk_size, 128 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
