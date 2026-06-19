"""Backend-роутер для генерации фоновой музыки.

Доступные бэкенды (выбирается через config.yaml → music.backend):
- stable_audio_open: Stable Audio Open 1.0 (default, легкий, ~10 сек/30с)
- ace_step:          ACE-Step 1.5 (продуктовый, lyrics/style/ref control)

Все бэкенды реализуют один протокол MusicBackend и возвращают MusicVariant.
"""
