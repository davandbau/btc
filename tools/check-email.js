#!/usr/bin/env node
// Usage: node check-email.js [--limit N] [--all]
// Defaults to unseen messages only, limit 10.

const { ImapFlow } = require('imapflow');
const fs = require('fs');
const path = require('path');

const creds = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '.email-creds.json'), 'utf8'));

const args = process.argv.slice(2);
const showAll = args.includes('--all');
let limit = 10;
const limIdx = args.indexOf('--limit');
if (limIdx !== -1 && args[limIdx + 1]) limit = parseInt(args[limIdx + 1], 10);

async function main() {
  const client = new ImapFlow({
    host: creds.imap.host,
    port: creds.imap.port,
    secure: creds.imap.secure,
    auth: { user: creds.email, pass: creds.password },
    logger: false
  });

  await client.connect();
  const lock = await client.getMailboxLock('INBOX');

  try {
    const status = await client.status('INBOX', { messages: true, unseen: true });
    console.log(`Inbox: ${status.messages} total, ${status.unseen} unread\n`);

    const query = showAll ? { all: true } : { unseen: true };
    let count = 0;

    for await (const msg of client.fetch(query, { envelope: true, bodyStructure: true, source: false })) {
      if (count >= limit) break;
      const env = msg.envelope;
      const from = env.from?.[0] ? `${env.from[0].name || ''} <${env.from[0].address}>` : 'unknown';
      console.log(`---`);
      console.log(`From: ${from}`);
      console.log(`Subject: ${env.subject}`);
      console.log(`Date: ${env.date}`);
      console.log(`UID: ${msg.uid}`);
      count++;
    }

    if (count === 0) console.log('No messages found.');
  } finally {
    lock.release();
    await client.logout();
  }
}

main().catch(err => { console.error(err.message); process.exit(1); });
