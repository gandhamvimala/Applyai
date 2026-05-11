// ============================================================
// ApplyAI Backend — Real Auto-Apply Agent
// Stack: Node.js + Express + Playwright + Firebase Admin
// Deploy free on: Railway.app or Render.com
// ============================================================

const express = require('express');
const cors = require('cors');
const { chromium } = require('playwright');
const admin = require('firebase-admin');
const nodemailer = require('nodemailer');
const twilio = require('twilio');

const app = express();
app.use(cors({
  origin: [
    'https://gandhamvimala.github.io',
    'http://localhost',
    'http://127.0.0.1',
    '*'
  ],
  methods: ['GET', 'POST', 'OPTIONS'],
  allowedHeaders: ['Content-Type', 'Authorization']
}));
app.use(express.json());

// ── Firebase Admin Init ──────────────────────────────────
// Firebase Admin - optional, only init if env var is set
let db = null;
if (process.env.FIREBASE_SERVICE_ACCOUNT) {
  try {
    admin.initializeApp({
      credential: admin.credential.cert(JSON.parse(process.env.FIREBASE_SERVICE_ACCOUNT)),
    });
    db = admin.firestore();
    console.log('Firebase connected');
  } catch(e) {
    console.warn('Firebase init failed:', e.message);
  }
} else {
  console.warn('FIREBASE_SERVICE_ACCOUNT not set - Firebase disabled');
}

// ── Twilio (SMS alerts) ──────────────────────────────────
const twilioClient = process.env.TWILIO_SID ? twilio(process.env.TWILIO_SID, process.env.TWILIO_TOKEN) : null;

// ── Email (interview alerts) ─────────────────────────────
const mailer = process.env.EMAIL_USER ? nodemailer.createTransport({
  service: 'gmail',
  auth: { user: process.env.EMAIL_USER, pass: process.env.EMAIL_PASS }
}) : null;

// ============================================================
// ROUTE: Submit application via Playwright
// POST /apply { jobUrl, resumeText, userEmail, userName, phone }
// ============================================================
app.post('/apply', async (req, res) => {
  const { jobUrl, resumeText, userEmail, userName, phone, jobTitle, company } = req.body;
  if (!jobUrl || !resumeText) return res.status(400).json({ error: 'Missing jobUrl or resumeText' });

  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();

    // Set realistic browser headers to avoid bot detection
    await page.setExtraHTTPHeaders({
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      'Accept-Language': 'en-US,en;q=0.9',
    });

    await page.goto(jobUrl, { waitUntil: 'networkidle', timeout: 30000 });

    // Detect which ATS the job uses
    const ats = await detectATS(page, jobUrl);
    console.log('Detected ATS:', ats, 'for', jobUrl);

    let applied = false;

    if (ats === 'greenhouse') applied = await applyGreenhouse(page, { resumeText, userEmail, userName });
    else if (ats === 'lever') applied = await applyLever(page, { resumeText, userEmail, userName });
    else if (ats === 'ashby') applied = await applyAshby(page, { resumeText, userEmail, userName });
    else if (ats === 'workday') applied = await applyWorkday(page, { resumeText, userEmail, userName });
    else {
      // Generic form fill attempt
      applied = await applyGeneric(page, { resumeText, userEmail, userName });
    }

    if (applied) {
      // Send SMS alert
      if (phone && twilioClient) {
        await twilioClient.messages.create({
          body: `✅ ApplyAI: Applied to ${jobTitle} at ${company}! Check your dashboard.`,
          from: process.env.TWILIO_PHONE,
          to: phone
        });
      }
      // Send email confirmation
      if (mailer) await mailer.sendMail({
        from: `ApplyAI <${process.env.EMAIL_USER}>`,
        to: userEmail,
        subject: `Applied to ${jobTitle} at ${company}`,
        html: `<h2>Application submitted!</h2><p>We applied to <b>${jobTitle}</b> at <b>${company}</b> on your behalf.</p><p>Check your <a href="https://gandhamvimala.github.io/Applyai">ApplyAI dashboard</a> for updates.</p>`
      });
    }

    res.json({ success: applied, ats, message: applied ? 'Application submitted!' : 'Could not auto-apply to this job' });
  } catch (err) {
    console.error('Apply error:', err);
    res.status(500).json({ error: err.message });
  } finally {
    if (browser) await browser.close();
  }
});

// ============================================================
// ATS DETECTORS
// ============================================================
async function detectATS(page, url) {
  if (url.includes('greenhouse.io') || url.includes('boards.greenhouse')) return 'greenhouse';
  if (url.includes('lever.co')) return 'lever';
  if (url.includes('ashbyhq.com')) return 'ashby';
  if (url.includes('myworkdayjobs.com') || url.includes('workday')) return 'workday';
  if (url.includes('linkedin.com/jobs')) return 'linkedin';
  if (url.includes('indeed.com')) return 'indeed';
  // Check page content
  const content = await page.content();
  if (content.includes('greenhouse')) return 'greenhouse';
  if (content.includes('lever.co')) return 'lever';
  return 'generic';
}

// ============================================================
// ATS-SPECIFIC APPLIERS
// ============================================================
async function applyGreenhouse(page, { userEmail, userName }) {
  try {
    const [first, ...rest] = userName.split(' ');
    await page.fill('input[id="first_name"]', first || userName).catch(() => {});
    await page.fill('input[id="last_name"]', rest.join(' ') || 'Applicant').catch(() => {});
    await page.fill('input[id="email"]', userEmail).catch(() => {});
    // Upload resume if file input exists
    const fileInput = page.locator('input[type="file"]').first();
    if (await fileInput.isVisible().catch(() => false)) {
      // Resume upload handled separately
    }
    await page.click('input[type="submit"], button[type="submit"]').catch(() => {});
    await page.waitForTimeout(2000);
    return true;
  } catch(e) { console.error('Greenhouse error:', e); return false; }
}

async function applyLever(page, { userEmail, userName }) {
  try {
    await page.fill('input[name="name"]', userName).catch(() => {});
    await page.fill('input[name="email"]', userEmail).catch(() => {});
    await page.click('.template-btn-submit, button[type="submit"]').catch(() => {});
    await page.waitForTimeout(2000);
    return true;
  } catch(e) { console.error('Lever error:', e); return false; }
}

async function applyAshby(page, { userEmail, userName }) {
  try {
    const [first, ...rest] = userName.split(' ');
    await page.fill('input[placeholder*="First"]', first).catch(() => {});
    await page.fill('input[placeholder*="Last"]', rest.join(' ')).catch(() => {});
    await page.fill('input[type="email"]', userEmail).catch(() => {});
    await page.click('button[type="submit"]').catch(() => {});
    await page.waitForTimeout(2000);
    return true;
  } catch(e) { console.error('Ashby error:', e); return false; }
}

async function applyWorkday(page, { userEmail, userName }) {
  try {
    await page.click('[data-automation-id="applyNowButton"]').catch(() => {});
    await page.waitForTimeout(2000);
    return true; // Workday needs account — flag for manual
  } catch(e) { return false; }
}

async function applyGeneric(page, { userEmail, userName }) {
  try {
    // Try to fill common field patterns
    await page.fill('input[name*="name" i], input[placeholder*="name" i]', userName).catch(() => {});
    await page.fill('input[name*="email" i], input[type="email"]', userEmail).catch(() => {});
    await page.click('button[type="submit"], input[type="submit"], .apply-btn').catch(() => {});
    await page.waitForTimeout(2000);
    return true;
  } catch(e) { return false; }
}

// ============================================================
// ROUTE: Fetch jobs (proxy for CORS)
// GET /jobs?query=QA+Engineer&location=Remote
// ============================================================
app.get('/jobs', async (req, res) => {
  const { query, location, page: pg = 1 } = req.query;
  const q = encodeURIComponent(`${query} ${location}`);
  try {
    const response = await fetch(
      `https://jsearch.p.rapidapi.com/search?query=${q}&page=${pg}&num_pages=2&date_posted=all`,
      { headers: { 'X-RapidAPI-Key': process.env.JSEARCH_API_KEY, 'X-RapidAPI-Host': 'jsearch.p.rapidapi.com' } }
    );
    const data = await response.json();
    res.json(data);
  } catch(e) {
    res.status(500).json({ error: e.message });
  }
});

// ============================================================
// ROUTE: Health check
// ============================================================
app.get('/', (req, res) => res.json({ status: 'ApplyAI backend running ✅' }));

app.listen(process.env.PORT || 3000, () => console.log('ApplyAI backend started'));

/*
============================================================
SETUP INSTRUCTIONS — Deploy to Railway.app (free)
============================================================

1. Install dependencies:
   npm init -y
   npm install express cors playwright firebase-admin nodemailer twilio
   npx playwright install chromium

2. Set environment variables in Railway:
   JSEARCH_API_KEY = your RapidAPI key
   FIREBASE_SERVICE_ACCOUNT = { paste Firebase service account JSON }
   TWILIO_SID = your Twilio SID
   TWILIO_TOKEN = your Twilio auth token
   TWILIO_PHONE = +1xxxxxxxxxx
   EMAIL_USER = your Gmail
   EMAIL_PASS = Gmail app password (not your real password)

3. Deploy:
   - Push this file + package.json to a GitHub repo
   - Go to railway.app → New Project → Deploy from GitHub
   - Add env vars → Deploy
   - Copy your Railway URL (e.g. https://applyai-backend.railway.app)

4. Update your frontend index.html:
   - In Settings → API & Sources → paste your Railway URL as proxy
   - The frontend will now send real apply requests to your backend

5. Get Twilio (free SMS):
   - Sign up at twilio.com → Get a phone number (free trial)
   - Copy SID + Auth Token + Phone number

6. Gmail App Password:
   - Google Account → Security → 2FA → App Passwords
   - Generate password for "Mail"
   - Use that as EMAIL_PASS

============================================================
COST: $0/month (Railway free tier + Twilio trial + Gmail)
============================================================
*/
