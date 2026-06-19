# Shorts Factory

Shorts Factory is a local video pipeline for turning user-provided long-form
videos into short vertical clips with subtitles, music options, SEO metadata,
YouTube upload, and TikTok upload support.

The application runs locally on the creator's own computer. Generated clips,
source videos, tokens, caches, and local outputs are not intended to be stored
in this repository.

## TikTok review website

The static website used for TikTok Developer review is in `docs/`.

After GitHub Pages is enabled for the `main` branch and `/docs` folder, these
URLs are used in TikTok Developer Portal:

- Web/Desktop URL: `https://YOUR_GITHUB_USERNAME.github.io/shorts-factory/`
- Terms of Service: `https://YOUR_GITHUB_USERNAME.github.io/shorts-factory/terms.html`
- Privacy Policy: `https://YOUR_GITHUB_USERNAME.github.io/shorts-factory/privacy.html`
- Redirect URI: `https://YOUR_GITHUB_USERNAME.github.io/shorts-factory/tiktok/callback.html`

See `github_pages_setup.md` for the step-by-step setup.

## Local secrets

Do not commit real API keys or OAuth credentials.

Public defaults live in `config.yaml`. Private local overrides should go into:

```text
config.local.yaml
```

Use `config.local.example.yaml` as a template. The local file is ignored by Git.

OAuth client files and tokens belong in:

```text
secrets/
cache/
```

Both directories are ignored by Git.

## Development checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
