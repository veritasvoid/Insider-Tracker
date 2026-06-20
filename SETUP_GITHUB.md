# GitHub Setup — One-Time Steps

## What this gives you
- **Live dashboard**: `https://YOUR_USERNAME.github.io/insider-tracker/`
- **Auto runs**: Every weekday at 9:30 AM ET (GitHub Actions cron)
- **Persistent data**: SQLite DB commits back to the repo after each run

---

## Step 1 — Create the GitHub repo

1. Go to https://github.com/new
2. Name it `insider-tracker`
3. Set **Private** (only you can see it)
4. Leave everything else unchecked — don't add README/gitignore
5. Click **Create repository**
6. Copy the SSH or HTTPS URL shown (e.g. `https://github.com/YOUR_USERNAME/insider-tracker.git`)

---

## Step 2 — Push your code

Open PowerShell in `C:\Users\jjami\OneDrive\Desktop\Insider_Tracker` and run:

```powershell
git init
git add .
git commit -m "feat: initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/insider-tracker.git
git push -u origin main
```

> **Note:** `config.yaml` is in `.gitignore` — it will NOT be pushed. Your API keys stay local.

---

## Step 3 — Add GitHub Secrets

Go to your repo on GitHub → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

Add these 5 secrets (exact names must match):

| Secret name | Value |
|-------------|-------|
| `ALPACA_API_KEY` | *(from your Alpaca dashboard → API Keys)* |
| `ALPACA_API_SECRET` | *(paired secret shown alongside the Alpaca API key)* |
| `POLYGON_API_KEY` | *(from your Polygon.io dashboard → API Keys)* |
| `GEMINI_API_KEY` | *(from Google AI Studio → Get API key)* |
| `ANTHROPIC_API_KEY` | *(from console.anthropic.com → API Keys)* |

> **Security note:** real key values used to be pasted directly into this file. They've been
> removed since this doc lives in the repo and anyone with read access could see them. If you
> haven't already, rotate (regenerate) all five keys at their respective providers — the old
> values are still visible in this repo's git history even after this edit.

---

## Step 4 — Enable GitHub Pages

1. Go to your repo → **Settings** → **Pages**
2. Under **Source**, select **Deploy from a branch**
3. Branch: `main`, Folder: `/docs`
4. Click **Save**
5. After ~1 minute, your dashboard is live at:
   `https://YOUR_USERNAME.github.io/insider-tracker/`

---

## Step 5 — Test the workflow manually

1. Go to your repo → **Actions** tab
2. Click **Daily Insider Tracker** (left sidebar)
3. Click **Run workflow** → **Run workflow**
4. Watch the run — should complete in ~3–5 minutes
5. After it finishes, open your Pages URL and confirm the dashboard updated

---

## After setup

The pipeline runs automatically every weekday at 9:30 AM ET.
You don't need to do anything — just check your dashboard URL whenever you want