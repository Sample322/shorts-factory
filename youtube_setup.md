# Настройка YouTube-загрузки за 7 минут

Нужно один раз получить OAuth client ID от Google и положить файл в `secrets/`. Дальше всё авторизуется в браузере одной кнопкой.

## Шаг 1. Создать проект в Google Cloud (2 мин)

1. Открой https://console.cloud.google.com/
2. Создай новый проект: верх → выбор проекта → «Новый проект» → имя `shorts-factory` → создать

## Шаг 2. Включить YouTube Data API v3 (1 мин)

1. Слева → **APIs & Services** → **Library**
2. Найди «YouTube Data API v3» → **Enable**

## Шаг 3. OAuth consent screen (2 мин)

1. Слева → **APIs & Services** → **OAuth consent screen**
2. User Type: **External** → Create
3. Заполни обязательное:
   - App name: `Shorts Factory`
   - User support email: твой email
   - Developer contact: твой email
4. **Scopes** → Add or Remove → найди `https://www.googleapis.com/auth/youtube.upload` → Update → Save and Continue
5. **Test users** → Add Users → добавь свой email (тот что владеет каналом)
6. Save and Continue → Back to Dashboard

> Режим «Testing» позволяет 100 уникальных пользователей. Для личного pipeline — навсегда хватит.

## Шаг 4. Создать OAuth client ID (1 мин)

1. Слева → **APIs & Services** → **Credentials**
2. Create Credentials → **OAuth client ID**
3. Application type: **Desktop app**
4. Name: `shorts-factory-desktop` → Create
5. В диалоге «OAuth client created» нажми **Download JSON** (значок ⬇)

## Шаг 5. Положить файл в проект

Скачанный файл (например `client_secret_XXX.json`) переименуй в `youtube_client_secret.json` и положи сюда:

```
C:\shorts-factory\secrets\youtube_client_secret.json
```

Папку `secrets/` создай, если её нет. **Не комитить этот файл в git!**

## Шаг 6. Авторизоваться (1 мин)

В Shorts Factory нажми кнопку **🔐 Авторизовать YouTube** в сайдбаре. Откроется браузер:

1. Выбери Google-аккаунт твоего канала
2. Появится «Google hasn't verified this app» — это нормально (testing mode). Жми **Advanced** → **Go to Shorts Factory (unsafe)**
3. Разреши «Manage your YouTube account»
4. Браузер скажет «The authentication flow has completed»

Готово. Токен сохранён в `cache/youtube_token.json`, обновляется автоматически.

## Что важно знать

- **Квота API**: 10 000 единиц/день по умолчанию. Загрузка одного видео = 1600 единиц → **~6 видео в день** на лимите. Если нужно больше — запрос увеличения квоты в Google Cloud (бесплатно, 1-2 дня на одобрение).
- **Privacy на первой загрузке**: проекты созданные после 28.07.2020 без верификации Google автоматически загружают как `private`, даже если ты выбрал `public`. Чтобы видео шли сразу публично — пройди verification (Google Cloud → APIs & Services → Audit). Для личного pipeline и теста подходит `unlisted`.
- **Custom thumbnails** требуют верифицированный канал (10k подписчиков или подтверждённый телефон в YouTube Studio).
- **Made for Kids = False**: мы сами это указываем для каждого видео, так как Shorts от взрослого контент-агрегатора. Если канал детский — поменяй в коде.

## Возможные проблемы

| Ошибка | Причина | Что делать |
|---|---|---|
| `403 quotaExceeded` | Закончилась дневная квота | Подождать сутки или запросить увеличение |
| `403 forbidden` | Не добавил себя в Test Users | Шаг 3 п.5 |
| `invalid_grant` | Старый токен битый | Нажми «🔁 Сбросить авторизацию» в UI |
| Канал не выбран | Аккаунт без канала на YouTube | Сначала создай канал на youtube.com |
