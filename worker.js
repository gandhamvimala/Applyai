// ============================================================
// ApplyAI — Cloudflare Worker Proxy for JSearch API
// Deploy at: https://workers.cloudflare.com
// This adds CORS headers so your GitHub Pages site can call
// the JSearch RapidAPI without browser blocking.
// ============================================================

const RAPIDAPI_KEY = '8a85c9ed3bmshb465717d10fa749p1b9438jsn52b1147d7149';
const RAPIDAPI_HOST = 'jsearch.p.rapidapi.com';

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
  'Content-Type': 'application/json',
};

export default {
  async fetch(request, env) {
    // Handle CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: CORS_HEADERS });
    }

    const url = new URL(request.url);
    const path = url.pathname; // e.g. /search or /job-details

    // Only allow specific JSearch endpoints
    const allowed = ['/search', '/search-v2', '/job-details', '/estimated-salary'];
    if (!allowed.some(p => path.startsWith(p))) {
      return new Response(JSON.stringify({ error: 'Endpoint not allowed' }), {
        status: 403,
        headers: CORS_HEADERS,
      });
    }

    // Forward query params to JSearch
    const jsearchUrl = `https://${RAPIDAPI_HOST}${path}${url.search}`;

    const response = await fetch(jsearchUrl, {
      method: 'GET',
      headers: {
        'X-RapidAPI-Key': RAPIDAPI_KEY,
        'X-RapidAPI-Host': RAPIDAPI_HOST,
        'Content-Type': 'application/json',
      },
    });

    const data = await response.json();

    return new Response(JSON.stringify(data), {
      status: response.status,
      headers: CORS_HEADERS,
    });
  },
};
