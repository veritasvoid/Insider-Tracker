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
| `ALPACA_API_KEY` | `PKHKFDI2FTO6QEFVOF2X7MEDHF` |
| `ALPACA_API_SECRET` | `EUZ4ccvf3gtgaeW1PSuWUDQCi4k23jXkJNExyx1KF2Zv` |
| `POLYGON_API_KEY` | `LXNy07eA1YCLjfGaeblfo_5MsDH12vKR` |
| `GEMINI_API_KEY` | `AQ.Ab8RN6IlO4UtQdtxynvJqWGPEWMJnY-xTI5ntICN0-HH8A8naw` |
| `ANTHROPIC_API_KEY` | `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` |

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
You don't need to do anything — just check your dashboard URL whenever you want.

To trigger an extra run manually: **Actions** → **Daily Insider Tracker** → **Run workflow**.

---

## Syncing local ↔ GitHub

After each Actions run, the DB and HTML are committed to the repo.
To pull those changes locally:
```powershell
cd C:\Users\jjami\OneDrive\Desktop\Insider_Tracker
git pull
```
