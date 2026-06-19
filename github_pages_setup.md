# GitHub Pages setup for TikTok review

Эта инструкция нужна, чтобы у локального проекта Shorts Factory появились
публичные HTTPS-ссылки для TikTok Developer Portal:

- Web/Desktop URL
- Terms of Service URL
- Privacy Policy URL
- TikTok OAuth Redirect URI

## Что уже лежит в проекте

Статический сайт находится в папке:

```text
C:\shorts-factory\docs
```

Страницы:

```text
docs/index.html
docs/terms.html
docs/privacy.html
docs/tiktok/callback.html
docs/style.css
```

## Важно перед публикацией

Не загружай в GitHub:

- `secrets/`
- `cache/`
- `output/`
- `logs/`
- `.venv/`
- `.venv-acestep/`
- исходные серии/фильмы
- готовые клипы
- музыку из личной папки
- OAuth-токены

Я уже добавил это в `.gitignore`, но все равно проверяй перед публикацией.

## Локальные API-ключи после очистки проекта

Публичный `config.yaml` больше не должен содержать реальные API-ключи. Если
позже захочешь снова включать Kimi/OpenRouter/Gemini/Groq, создай локальный
файл:

```text
C:\shorts-factory\config.local.yaml
```

Можно скопировать шаблон:

```text
C:\shorts-factory\config.local.example.yaml
```

`config.local.yaml` игнорируется Git и автоматически перекрывает значения из
`config.yaml`.

## Вариант A: через GitHub Desktop

Это самый простой путь без командной строки.

1. Установи GitHub Desktop:
   https://desktop.github.com/
2. Войди в свой GitHub аккаунт.
3. Нажми `File` -> `Add local repository`.
4. Выбери папку:

```text
C:\shorts-factory
```

5. Если GitHub Desktop скажет, что это не git repository, нажми
   `create a repository`.
6. Repository name:

```text
shorts-factory
```

7. Local path должен остаться:

```text
C:\shorts-factory
```

8. Сделай первый commit, например:

```text
Add Shorts Factory app and GitHub Pages site
```

9. Нажми `Publish repository`.
10. Лучше оставить репозиторий `Private`, если GitHub Pages доступен на твоем
    тарифе для private repo. Если Pages не даст включить сайт у private repo,
    придется сделать repo public или вынести только папку `docs` в отдельный
    public repo.

## Вариант B: через командную строку

Открой PowerShell в папке проекта:

```powershell
cd C:\shorts-factory
git init
git add .
git commit -m "Add Shorts Factory app and GitHub Pages site"
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/shorts-factory.git
git push -u origin main
```

Перед этим на GitHub нужно создать пустой repository с именем:

```text
shorts-factory
```

В команде замени `YOUR_GITHUB_USERNAME` на свой логин GitHub.

## Включить GitHub Pages

1. Открой repository на GitHub.
2. Перейди в `Settings`.
3. Слева открой `Pages`.
4. В `Build and deployment` выбери:

```text
Source: Deploy from a branch
Branch: main
Folder: /docs
```

5. Нажми `Save`.
6. Подожди 1-5 минут.

GitHub покажет ссылку вида:

```text
https://YOUR_GITHUB_USERNAME.github.io/shorts-factory/
```

## Какие URL вставлять в TikTok Developer Portal

Если repo называется `shorts-factory`, а GitHub username `YOUR_GITHUB_USERNAME`,
то URL будут такими:

### Web/Desktop URL

```text
https://YOUR_GITHUB_USERNAME.github.io/shorts-factory/
```

### Terms of Service URL

```text
https://YOUR_GITHUB_USERNAME.github.io/shorts-factory/terms.html
```

### Privacy Policy URL

```text
https://YOUR_GITHUB_USERNAME.github.io/shorts-factory/privacy.html
```

### Redirect URI для Login Kit

```text
https://YOUR_GITHUB_USERNAME.github.io/shorts-factory/tiktok/callback.html
```

Именно этот же Redirect URI нужно записать в:

```text
C:\shorts-factory\secrets\tiktok_client.json
```

Пример:

```json
{
  "client_key": "PASTE_CLIENT_KEY",
  "client_secret": "PASTE_CLIENT_SECRET",
  "redirect_uri": "https://YOUR_GITHUB_USERNAME.github.io/shorts-factory/tiktok/callback.html"
}
```

## Что делать для demo video

TikTok хочет увидеть полный end-to-end flow. Это не рекламный ролик, а запись
экрана для ревью.

Запиши видео на 2-4 минуты:

1. Открой GitHub Pages сайт Shorts Factory.
2. Покажи, что есть главная страница, Privacy Policy и Terms of Service.
3. Открой локальный Shorts Factory на ПК.
4. Покажи блок TikTok в сайдбаре.
5. Нажми ссылку авторизации TikTok.
6. Покажи TikTok consent screen.
7. После редиректа покажи страницу `tiktok/callback.html`.
8. Скопируй code или полный URL.
9. Вернись в Shorts Factory и вставь code/URL.
10. Нажми `Сохранить TikTok-токен`.
11. Включи TikTok auto-upload.
12. Запусти завод на коротком тестовом видео.
13. Покажи, что после рендера появляется `tiktok_publish_id` и статус.
14. Если TikTok открыл видео как draft/private, покажи это в TikTok.

Для demo video лучше использовать нейтральный тестовый ролик, на который у тебя
есть права. Не используй серию сериала или фильм в ревью-демо.

## Что писать в App Review

Если используешь Direct Post и scope `video.publish`:

```text
Shorts Factory is a local web/desktop tool used by the app owner to prepare short vertical videos from user-provided media and publish them to the owner's TikTok account. Login Kit is used only to let the owner authorize the app and create an OAuth access/refresh token. The user.info.basic scope is used to identify the authorized TikTok account and confirm the correct creator is connected. The Content Posting API is used after the user explicitly enables TikTok auto-upload in the tool. The video.publish scope uploads the final MP4 file from the local machine to the authorized TikTok account with the selected privacy, caption, and interaction settings. The app does not read feeds, manage other users, or post without the owner enabling upload.
```

Если TikTok пока дает только `video.upload`:

```text
Shorts Factory is a local web/desktop tool used by the app owner to prepare short vertical videos from user-provided media and upload them to the owner's TikTok account. Login Kit is used only to let the owner authorize the app and create an OAuth access/refresh token. The user.info.basic scope is used to identify the authorized TikTok account and confirm the correct creator is connected. The Content Posting API uses video.upload to send the final MP4 file to the creator's TikTok inbox/drafts so the creator can review, edit, and manually publish it in TikTok. The app does not read feeds, manage other users, or publish without user action.
```
