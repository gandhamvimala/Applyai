const express = require('express');
const cors = require('cors');
const { Pool } = require('pg');

const app = express();
app.use(cors({ origin: '*' }));
app.use(express.json({ limit: '10mb' }));

// ── Postgres (Railway provides DATABASE_URL automatically) ──
const pool = new Pool({
  connectionString: process.env.DATABASE_URL || 'postgresql://postgres:JTHvziAHPXvruHoRhcTRknvMVNBFsrJq@yamabiko.proxy.rlwy.net:13979/railway',
  ssl: { rejectUnauthorized: false }
});

// ── Create tables on startup ──
async function initDB() {
  try {
    await pool.query(`
      CREATE TABLE IF NOT EXISTS users (
        uid TEXT PRIMARY KEY,
        name TEXT,
        email TEXT,
        resume_name TEXT,
        resume_text TEXT,
        resume_date TEXT,
        applications JSONB DEFAULT '[]',
        interviews JSONB DEFAULT '[]',
        prefs JSONB DEFAULT '{}',
        applied_count INTEGER DEFAULT 0,
        updated_at TIMESTAMPTZ DEFAULT NOW()
      )
    `);
    console.log('DB ready');
  } catch(e) {
    console.error('DB init error:', e.message);
  }
}
initDB();

// ── ROUTE: Health check ──
app.get('/', (req, res) => res.json({ status: 'ApplyAI backend running OK' }));

// ── ROUTE: Save user data ──
app.post('/user/:uid', async (req, res) => {
  const { uid } = req.params;
  const data = req.body;
  try {
    await pool.query(`
      INSERT INTO users (uid, name, email, resume_name, resume_text, resume_date, applications, interviews, prefs, applied_count, updated_at)
      VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW())
      ON CONFLICT (uid) DO UPDATE SET
        name = COALESCE($2, users.name),
        email = COALESCE($3, users.email),
        resume_name = COALESCE($4, users.resume_name),
        resume_text = COALESCE($5, users.resume_text),
        resume_date = COALESCE($6, users.resume_date),
        applications = COALESCE($7, users.applications),
        interviews = COALESCE($8, users.interviews),
        prefs = COALESCE($9, users.prefs),
        applied_count = COALESCE($10, users.applied_count),
        updated_at = NOW()
    `, [
      uid,
      data.name || null,
      data.email || null,
      data.resumeName || null,
      data.resumeText || null,
      data.resumeDate || null,
      data.applications ? JSON.stringify(data.applications) : null,
      data.interviews ? JSON.stringify(data.interviews) : null,
      data.prefs ? JSON.stringify(data.prefs) : null,
      data.appliedCount || null
    ]);
    res.json({ success: true });
  } catch(e) {
    console.error('Save error:', e.message);
    res.status(500).json({ error: e.message });
  }
});

// ── ROUTE: Load user data ──
app.get('/user/:uid', async (req, res) => {
  const { uid } = req.params;
  try {
    const result = await pool.query('SELECT * FROM users WHERE uid = $1', [uid]);
    if (result.rows.length === 0) return res.json({ exists: false });
    const row = result.rows[0];
    res.json({
      exists: true,
      name: row.name,
      email: row.email,
      resumeName: row.resume_name,
      resumeText: row.resume_text,
      resumeDate: row.resume_date,
      applications: row.applications || [],
      interviews: row.interviews || [],
      prefs: row.prefs || {},
      appliedCount: row.applied_count || 0
    });
  } catch(e) {
    console.error('Load error:', e.message);
    res.status(500).json({ error: e.message });
  }
});

// ── ROUTE: JSearch proxy (fixes CORS) ──
app.get('/search', async (req, res) => {
  const apiKey = process.env.JSEARCH_API_KEY;
  if (!apiKey) return res.status(500).json({ error: 'JSEARCH_API_KEY not set' });
  try {
    const params = new URLSearchParams(req.query).toString();
    const response = await fetch(`https://jsearch.p.rapidapi.com/search?${params}`, {
      headers: {
        'X-RapidAPI-Key': apiKey,
        'X-RapidAPI-Host': 'jsearch.p.rapidapi.com'
      }
    });
    const data = await response.json();
    res.json(data);
  } catch(e) {
    res.status(500).json({ error: e.message });
  }
});

app.listen(process.env.PORT || 3000, () => console.log('ApplyAI backend started on port', process.env.PORT || 3000));
