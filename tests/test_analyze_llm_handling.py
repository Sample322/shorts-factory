import json
import unittest
from pathlib import Path

from pipeline.analyze import (
    Segment,
    _apply_time_offset,
    _build_analysis_prompt,
    _looks_like_garbage,
    _is_ollama_resource_error,
    _ollama_analysis_option_attempts,
    _parse_response,
    _select_evenly,
    _split_local_analysis_chunks,
    describe_llm_chain,
)


def _clip(start: float, end: float, title: str) -> dict:
    return {
        "start": start,
        "end": end,
        "title": title,
        "hook": title,
        "description": title,
        "tags": ["test"],
        "music_mood": "comedy",
        "reason": "good hook",
    }


class AnalyzeLlmHandlingTest(unittest.TestCase):
    def test_valid_repetitive_json_is_not_garbage(self) -> None:
        raw = json.dumps({
            "clips": [
                _clip(20, 55, "clip one"),
                _clip(90, 125, "clip two"),
                _clip(160, 195, "clip three"),
                _clip(230, 265, "clip four"),
            ]
        })

        self.assertFalse(_looks_like_garbage(raw))
        self.assertEqual(len(_parse_response(raw)), 4)

    def test_balanced_json_is_parsed_before_trailing_noise(self) -> None:
        raw = json.dumps({"clips": [_clip(20, 55, "clip one")]})
        raw += "\n\n" + ('"}"]}' * 20)

        segments = _parse_response(raw)

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].title, "clip one")

    def test_short_but_valid_clip_is_expanded_to_target_duration(self) -> None:
        raw = json.dumps({"clips": [_clip(45, 60, "short clip")]})

        segments = _parse_response(raw, target_duration=40, min_duration=15)

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].start, 45)
        self.assertEqual(segments[0].end, 85)

    def test_chunk_prompt_uses_local_seconds_and_offsets_results(self) -> None:
        cfg = {
            "shorts": {
                "min_duration_sec": 15,
                "max_duration_sec": 60,
                "target_duration_sec": 40,
                "target_min_ratio": 0.8,
                "target_max_ratio": 1.25,
            }
        }
        template = "{clips_count}|{target_sec}|{total_seconds}\n{transcript}"
        prompt = _build_analysis_prompt(
            template,
            [{"start": 1373.7, "end": 1379.2, "text": "hello there"}],
            6,
            cfg,
            6,
            time_offset=1373.7,
        )

        shifted = _apply_time_offset(
            [Segment(start=24, end=64, title="local clip")], 1373.7
        )

        self.assertIn("[0s] hello there", prompt)
        self.assertIn("LOCAL chunk seconds", prompt)
        self.assertAlmostEqual(shifted[0].start, 1397.7)
        self.assertAlmostEqual(shifted[0].end, 1437.7)

    def test_even_selection_skips_overlapping_candidates(self) -> None:
        first = [
            Segment(start=10, end=50, title="a1"),
            Segment(start=20, end=60, title="a2 overlaps"),
            Segment(start=80, end=120, title="a3"),
        ]
        second = [
            Segment(start=130, end=170, title="b1"),
            Segment(start=135, end=175, title="b2 overlaps"),
        ]

        selected = _select_evenly([first, second], 4, min_gap_sec=10)

        self.assertEqual([s.title for s in selected], ["a1", "b1", "a3"])

    def test_obvious_looped_response_is_garbage(self) -> None:
        raw = '{"clips":[' + ("broken_loop_token" * 12)

        self.assertTrue(_looks_like_garbage(raw))

    def test_llm_chain_label_reflects_enabled_layers(self) -> None:
        cfg = {
            "kimi": {"enabled": False, "api_key": "present-but-disabled"},
            "gemini": {"enabled": False, "api_key": ""},
            "groq": {"enabled": False, "api_key": ""},
            "openrouter": {"enabled": True, "api_key": "test-openrouter-key"},
            "ollama": {"primary_model": "qwen2.5:14b", "fallback_model": "qwen3:14b"},
        }

        self.assertEqual(describe_llm_chain(cfg), "OpenRouter → Ollama")

    def test_analysis_prompt_does_not_seed_specific_character_names(self) -> None:
        prompt = Path("prompts/analyze_segments.md").read_text(encoding="utf-8")

        self.assertIn("Не выдумывай имена персонажей", prompt)
        self.assertNotIn("Чендлер", prompt)
        self.assertNotIn("Росс", prompt)
        self.assertNotIn("Дженис", prompt)

    def test_local_analysis_chunks_by_video_boundaries(self) -> None:
        transcript = {
            "segments": [
                {"start": 1.0, "end": 3.0, "text": "one", "source_path": "a.mkv"},
                {"start": 11.0, "end": 13.0, "text": "two", "source_path": "b.mkv"},
                {"start": 21.0, "end": 23.0, "text": "three", "source_path": "c.mkv"},
            ],
            "video_boundaries": [
                {"path": "a.mkv", "offset": 0.0, "duration": 10.0},
                {"path": "b.mkv", "offset": 10.0, "duration": 10.0},
                {"path": "c.mkv", "offset": 20.0, "duration": 10.0},
            ],
        }

        chunks = _split_local_analysis_chunks(
            transcript, {"ollama": {"analysis_chunk_max_chars": 22000}}
        )

        self.assertEqual([len(chunk) for chunk in chunks], [1, 1, 1])

    def test_ollama_analysis_options_include_lighter_retry(self) -> None:
        cfg = {
            "ollama": {
                "temperature": 0.2,
                "num_ctx": 8192,
                "num_predict": 2048,
                "analysis_retry_num_ctx": [4096],
                "analysis_retry_num_predict": 1024,
            }
        }

        attempts = _ollama_analysis_option_attempts(cfg)

        self.assertEqual(
            attempts,
            [
                {"temperature": 0.2, "num_predict": 2048, "num_ctx": 8192},
                {"temperature": 0.2, "num_predict": 1024, "num_ctx": 4096},
            ],
        )

    def test_ollama_resource_error_detection_catches_cuda(self) -> None:
        self.assertTrue(_is_ollama_resource_error("CUDA error: unknown error"))
        self.assertTrue(_is_ollama_resource_error("llama-server process has terminated"))
        self.assertFalse(_is_ollama_resource_error("invalid JSON or no clip objects"))


if __name__ == "__main__":
    unittest.main()
