import unittest
from unittest.mock import patch

from pipeline.seo_generator import _build_prompt, _metadata_is_grounded, generate_seo


class FakeOllamaClient:
    calls: list[dict] = []

    def __init__(self, host: str, **kwargs) -> None:
        self.host = host

    def list(self) -> dict:
        return {"models": [{"model": "qwen2.5:14b-instruct-q4_K_M"}]}

    def chat(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {
            "message": {
                "content": (
                    '{"title":"Леха спорит с учителем. СашаТаня S2",'
                    '"description":"Леха пытается выкрутиться в классе, но ситуация становится еще смешнее.\\n'
                    'Сцена из сериала СашаТаня, 2 сезон.\\n\\n'
                    '#shorts #сериал #момент",'
                    '"tags":["СашаТаня","Леха","сцена из сериала","комедия","русский сериал"],'
                    '"category_id":"24"}'
                )
            }
        }


class FakeHallucinatingOllamaClient(FakeOllamaClient):
    def chat(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {
            "message": {
                "content": (
                    '{"title":"Я тот, кто стучится | Breaking Bad S4",'
                    '"description":"Сцена из Breaking Bad с Walter White.\\n\\n'
                    '#shorts #сериал #момент",'
                    '"tags":["Breaking Bad","Walter White","tv series scene"],'
                    '"category_id":"24"}'
                )
            }
        }


class SeoGeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeOllamaClient.calls = []
        FakeHallucinatingOllamaClient.calls = []

    def test_prompt_forbids_hallucinated_source_data(self) -> None:
        prompt = _build_prompt(
            clip_title="Леха хочет перейти в другой класс",
            clip_description="Леха спорит с учителем и пытается выкрутиться.",
            clip_hook="Я в этот класс не пойду",
            clip_tags_hint=["Леха", "школа"],
            music_mood="comedy",
            source_context='Сериал "СашаТаня", 2 сезон',
            language="ru",
        )

        self.assertIn("Не выдумывай название, год, сезон", prompt)
        self.assertIn("Не делай клип “киношным трейлером”", prompt)
        self.assertIn("главный удерживающий элемент", prompt)

    def test_generate_seo_uses_local_ollama_when_cloud_is_disabled(self) -> None:
        cfg = {
            "kimi": {"enabled": False, "api_key": ""},
            "gemini": {"enabled": False, "api_key": ""},
            "groq": {"enabled": False, "api_key": ""},
            "openrouter": {"enabled": False, "api_key": ""},
            "ollama": {
                "host": "http://localhost:11434",
                "primary_model": "qwen2.5:14b-instruct-q4_K_M",
                "fallback_model": "qwen3:14b",
                "seo_num_ctx": 8192,
                "seo_num_predict": 2048,
                "seo_temperature": 0.35,
            },
            "seo": {
                "enabled": True,
                "description_style": "competitor_balanced",
                "cta": "Подпишись, тут самые лучшие сцены твоих любимых сериалов 🔥",
                "first_line_hashtags": 9,
                "max_description_hashtags": 22,
                "max_hidden_tags": 18,
                "include_youtube_shorts_hashtag": True,
                "include_russian_shorts_hashtag": True,
                "include_growth_hashtags": False,
                "uppercase_opening_hook": True,
            },
        }

        with patch("pipeline.seo_generator.ollama.Client", FakeOllamaClient):
            meta = generate_seo(
                cfg,
                clip_title="Леха хочет перейти в другой класс",
                clip_description="Леха спорит с учителем и пытается выкрутиться.",
                clip_hook="Я в этот класс не пойду",
                clip_tags_hint=["Леха", "школа"],
                music_mood="comedy",
                source_context='Сериал "СашаТаня", 2 сезон',
                language="ru",
            )

        self.assertEqual(len(FakeOllamaClient.calls), 1)
        self.assertEqual(
            FakeOllamaClient.calls[0]["model"],
            "qwen2.5:14b-instruct-q4_K_M",
        )
        self.assertEqual(FakeOllamaClient.calls[0]["format"], "json")
        self.assertEqual(FakeOllamaClient.calls[0]["options"]["num_ctx"], 8192)
        self.assertTrue(meta.title.startswith("😂 "))
        self.assertIn("#shorts", meta.description)
        self.assertIn("#шортс", meta.description)
        self.assertIn("#youtubeshorts", meta.description)
        self.assertIn("#сашатаня", meta.description)
        self.assertIn("#sashatanya", meta.description)
        self.assertIn("Подпишись", meta.description)
        self.assertNotIn("#viralshorts", meta.description)
        self.assertNotIn("#trendingshorts", meta.description)
        self.assertIn("СашаТаня", meta.tags)
        self.assertGreater(len(meta.tags), 8)

    def test_growth_hashtags_are_config_gated(self) -> None:
        cfg = {
            "kimi": {"enabled": False, "api_key": ""},
            "gemini": {"enabled": False, "api_key": ""},
            "groq": {"enabled": False, "api_key": ""},
            "openrouter": {"enabled": False, "api_key": ""},
            "ollama": {
                "host": "http://localhost:11434",
                "primary_model": "qwen2.5:14b-instruct-q4_K_M",
                "fallback_model": "",
                "seo_num_ctx": 8192,
                "seo_num_predict": 2048,
                "seo_temperature": 0.35,
            },
            "seo": {
                "enabled": True,
                "description_style": "competitor_balanced",
                "first_line_hashtags": 9,
                "max_description_hashtags": 30,
                "max_hidden_tags": 18,
                "include_youtube_shorts_hashtag": True,
                "include_russian_shorts_hashtag": True,
                "include_growth_hashtags": True,
            },
        }

        with patch("pipeline.seo_generator.ollama.Client", FakeOllamaClient):
            meta = generate_seo(
                cfg,
                clip_title="Леха хочет перейти в другой класс",
                clip_description="Леха спорит с учителем и пытается выкрутиться.",
                clip_hook="Я в этот класс не пойду",
                clip_tags_hint=["Леха", "школа"],
                music_mood="comedy",
                source_context='Сериал "СашаТаня", 2 сезон',
                language="ru",
            )

        self.assertIn("#viralshorts", meta.description)
        self.assertIn("#trendingshorts", meta.description)

    def test_config_can_skip_seo_llm_and_still_build_competitor_metadata(self) -> None:
        cfg = {
            "seo": {
                "enabled": True,
                "llm_enabled": False,
                "description_style": "competitor_balanced",
                "cta": "Подпишись, тут самые лучшие сцены твоих любимых сериалов 🔥",
                "first_line_hashtags": 9,
                "max_description_hashtags": 22,
                "max_hidden_tags": 18,
                "include_youtube_shorts_hashtag": True,
                "include_russian_shorts_hashtag": True,
                "include_growth_hashtags": False,
                "uppercase_opening_hook": True,
            },
            "ollama": {
                "host": "http://localhost:11434",
                "primary_model": "qwen2.5:14b-instruct-q4_K_M",
            },
        }

        with patch("pipeline.seo_generator.ollama.Client") as client_cls:
            meta = generate_seo(
                cfg,
                clip_title="Леха хочет перейти в другой класс",
                clip_description="Леха спорит с учителем и пытается выкрутиться.",
                clip_hook="Я в этот класс не пойду",
                clip_tags_hint=["Леха", "школа"],
                music_mood="comedy",
                source_context='Сериал "СашаТаня", 2 сезон',
                language="ru",
            )

        client_cls.assert_not_called()
        first_line = meta.description.splitlines()[0]
        self.assertIn("#shorts", first_line)
        self.assertIn("#шортс", first_line)
        self.assertIn("#youtubeshorts", first_line)
        self.assertIn("#сашатаня", meta.description)
        self.assertNotIn("#моментизфильма", meta.description)
        self.assertNotIn("момент из фильма", meta.tags)
        self.assertNotIn("фильм", meta.tags)

    def test_hallucinated_ollama_seo_is_rejected(self) -> None:
        cfg = {
            "kimi": {"enabled": False, "api_key": ""},
            "gemini": {"enabled": False, "api_key": ""},
            "groq": {"enabled": False, "api_key": ""},
            "openrouter": {"enabled": False, "api_key": ""},
            "ollama": {
                "host": "http://localhost:11434",
                "primary_model": "qwen2.5:14b-instruct-q4_K_M",
                "fallback_model": "",
                "seo_num_ctx": 8192,
                "seo_num_predict": 2048,
                "seo_temperature": 0.35,
            },
        }

        with patch("pipeline.seo_generator.ollama.Client", FakeHallucinatingOllamaClient):
            meta = generate_seo(
                cfg,
                clip_title="Леха хочет перейти в другой класс",
                clip_description="Леха спорит с учителем и пытается выкрутиться.",
                clip_hook="Я в этот класс не пойду",
                clip_tags_hint=["Леха", "школа"],
                music_mood="comedy",
                source_context='Сериал "СашаТаня", 2 сезон',
                language="ru",
            )

        combined = " ".join([meta.title, meta.description, " ".join(meta.tags)])
        self.assertNotIn("Breaking Bad", combined)
        self.assertNotIn("Walter White", combined)
        self.assertIn("СашаТаня", combined)

    def test_placeholder_seo_is_rejected(self) -> None:
        ok, reason = _metadata_is_grounded(
            {
                "title": 'Смешной момент из сериала "???????" [сезон]',
                "description": "Год выпуска: [год].",
                "tags": ["???????"],
            },
            source_context='Сериал "СашаТаня", 2 сезон',
            clip_title="Леха хочет перейти в другой класс",
            clip_description="Леха спорит с учителем.",
            clip_tags_hint=["Леха"],
        )

        self.assertFalse(ok)
        self.assertIn("плейсхолдеры", reason)


if __name__ == "__main__":
    unittest.main()
