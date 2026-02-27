#!/usr/bin/env node
// Google Search via Serper.dev API
// Usage: node google-search.js "query" [--num 10] [--type search|news|images]

const fs = require('fs');
const path = require('path');

const creds = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '.serper-creds.json'), 'utf8'));

async function search(query, options = {}) {
  const type = options.type || 'search';
  const num = options.num || 10;
  
  const endpoint = type === 'search' ? 'https://google.serper.dev/search' 
    : type === 'news' ? 'https://google.serper.dev/news'
    : type === 'images' ? 'https://google.serper.dev/images'
    : 'https://google.serper.dev/search';

  const res = await fetch(endpoint, {
    method: 'POST',
    headers: {
      'X-API-KEY': creds.apiKey,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ q: query, num })
  });

  if (!res.ok) {
    console.error(`Error: ${res.status} ${res.statusText}`);
    process.exit(1);
  }

  return res.json();
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 0) {
    console.error('Usage: node google-search.js "query" [--num 10] [--type search|news|images]');
    process.exit(1);
  }

  const query = args[0];
  let num = 10, type = 'search';
  
  for (let i = 1; i < args.length; i++) {
    if (args[i] === '--num' && args[i+1]) { num = parseInt(args[++i]); }
    if (args[i] === '--type' && args[i+1]) { type = args[++i]; }
  }

  const result = await search(query, { num, type });
  console.log(JSON.stringify(result, null, 2));
}

main().catch(err => { console.error(err); process.exit(1); });
