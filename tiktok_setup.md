# TikTok Auto Upload Setup

Эта интеграция использует официальный TikTok Content Posting API Direct Post.
Клипы отправляются в TikTok сразу после рендера, если в сайдбаре включена
автозагрузка.

## Что уже подготовлено в заводе

- `pipeline/tiktok_upload.py` — OAuth, refresh token, Direct Post upload,
  chunked file upload и проверка статуса публикации.
- `app.py` — блок TikTok в сайдбаре, авторизация, тумблер автозагрузки,
  выбор приватности и ручная загрузка уже готовых клипов.
- `pipeline/render.py` — после рендера клип может уходить в YouTube и TikTok.
- `cache/tiktok_token.json` — локальный token cache, не публикуется.
- `secrets/tiktok_client.json` — локальный файл с TikTok app credentials.

## Важное ограничение TikTok

Публичная автопубликация через Direct Post требует одобрения приложения
TikTok и scope `video.publish`. Пока приложение не прошло audit, TikTok может
ограничивать Direct Post private-режимом. Поэтому завод по умолчанию ставит
TikTok privacy в `SELF_ONLY`.

## 1. Создать TikTok Developer App

1. Открой https://developers.tiktok.com.
2. Войди в аккаунт, на который хочешь публиковать.
3. Создай приложение.
4. Добавь продукты:
   - Login Kit
   - Content Posting API
5. В Content Posting API включи Direct Post.
6. Запроси/получи доступ к scope:
   - `video.publish`
   - `user.info.basic`

## 2. Настроить Redirect URI

В Login Kit укажи HTTPS Redirect URI. TikTok не принимает обычный локальный
`http://localhost` для web OAuth.

Для локального проекта используй GitHub Pages. Я подготовил статический сайт в
`docs/`. Полная инструкция по публикации и нужным URL лежит здесь:

```text
C:\shorts-factory\github_pages_setup.md
```

Для нашего завода сервер на callback URL не обязателен: после редиректа ты
копируешь code или весь URL из браузера и вставляешь его в сайдбар.

Пример:

```text
https://YOUR_GITHUB_USERNAME.github.io/shorts-factory/tiktok/callback.html
```

Этот же URL нужно будет записать в `secrets/tiktok_client.json`.

## 3. Создать secrets/tiktok_client.json

Создай файл:

```text
C:\shorts-factory\secrets\tiktok_client.json
```

Содержимое:

```json
{
  "client_key": "PASTE_CLIENT_KEY",
  "client_secret": "PASTE_CLIENT_SECRET",
  "redirect_uri": "https://YOUR_GITHUB_USERNAME.github.io/shorts-factory/tiktok/callback.html"
}
```

Где взять значения:

- `client_key` — в TikTok Developer Portal, карточка приложения.
- `client_secret` — там же.
- `redirect_uri` — ровно тот HTTPS URL, который добавлен в Login Kit.

## 4. Авторизовать аккаунт в заводе

1. Запусти завод.
2. В сайдбаре открой блок TikTok.
3. Нажми ссылку авторизации TikTok.
4. Подтверди доступ.
5. После редиректа скопируй весь URL из адресной строки.
6. Вставь его в поле `Code или полный redirect URL`.
7. Нажми `Сохранить TikTok-токен`.

После этого завод создаст:

```text
C:\shorts-factory\cache\tiktok_token.json
```

Этот файл хранит access/refresh token локально на твоем ПК.

## 5. Включить автозагрузку

В сайдбаре:

1. TikTok → `Авто-загрузка`.
2. Выбери `SELF_ONLY` для тестов.
3. После TikTok audit можно выбрать `PUBLIC_TO_EVERYONE`.
4. Запусти завод как обычно.

Если включены и YouTube, и TikTok, каждый готовый клип будет отправлен в обе
платформы последовательно.

## 6. Где смотреть результат

После работы завода в `output/<job_id>/meta.json` у каждого клипа появятся поля:

```json
{
  "tiktok_publish_id": "...",
  "tiktok_status": "PUBLISH_COMPLETE",
  "tiktok_meta": {
    "caption": "...",
    "privacy_level": "SELF_ONLY"
  }
}
```

Иногда `tiktok_status` может остаться в processing-состоянии: TikTok может
дольше модерировать public-посты. Это не значит, что завод завис.

## Полезные официальные страницы

- Content Posting API Direct Post:
  https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
- Get Started Direct Post:
  https://developers.tiktok.com/doc/content-posting-api-get-started
- OAuth token management:
  https://developers.tiktok.com/doc/oauth-user-access-token-management
- Get Post Status:
  https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status
