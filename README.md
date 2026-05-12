# ApplyAI 🚀

Your AI-powered job application agent — pulls real jobs from LinkedIn, Indeed & Glassdoor and auto-applies.

## Live Demo
Once deployed: `https://YOUR-USERNAME.github.io/applyai`

---

## Deploy in 5 minutes

### Step 1 — Push to GitHub Pages

```bash
# 1. Create a new repo on github.com named: applyai
# 2. Then run:

git init
git add .
git commit -m "Initial ApplyAI deploy"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/applyai.git
git push -u origin main
```

Then go to your repo → **Settings → Pages → Source: main branch / root** → Save.

Your site will be live at: `https://YOUR-USERNAME.github.io/applyai`

---

### Step 2 — Deploy the Cloudflare Worker (fixes CORS for live jobs)

This lets your GitHub Pages site call the JSearch API without CORS errors.

1. Go to [workers.cloudflare.com](https://workers.cloudflare.com) — free account
2. Click **Create Worker**
3. Paste the contents of `worker.js` into the editor
4. Click **Save & Deploy**
5. Copy your Worker URL (e.g. `https://applyai-proxy.YOUR-NAME.workers.dev`)
6. Open your live site → **Settings → API & Sources**
7. Paste the Worker URL into the **Cloudflare Worker Proxy URL** field → Save

✅ Live jobs from LinkedIn, Indeed, Glassdoor will now load instantly.

---

## Files

| File | Purpose |
|------|---------|
| `index.html` | Full app — landing, auth, dashboard, job feed, all pages |
| `worker.js` | Cloudflare Worker proxy — fixes CORS for JSearch API |
| `README.md` | This file |

## Tech Stack

- **Frontend**: Vanilla HTML/CSS/JS — zero dependencies, zero build step
- **Job data**: [JSearch API](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) via RapidAPI (LinkedIn + Indeed + Glassdoor)
- **Proxy**: Cloudflare Workers (free tier — 100k requests/day)
- **Hosting**: GitHub Pages (free)

## Job Sources (via JSearch)
- LinkedIn Jobs
- Indeed
- Glassdoor
- ZipRecruiter
- + 20 more boards

## Features
- Upload resume → AI tailors it per job description
- Auto-applies to matched jobs
- Email + SMS notifications on interview requests
- Full dashboard: applications tracker, interview calendar, analytics
- Resume score + AI-tailored versions per company
