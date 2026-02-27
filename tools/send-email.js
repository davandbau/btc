#!/usr/bin/env node
// Usage: node send-email.js <to> <subject> <body> [--html]
// If --html flag is present, body is treated as HTML.

const nodemailer = require('nodemailer');
const fs = require('fs');
const path = require('path');

const creds = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '.email-creds.json'), 'utf8'));

const args = process.argv.slice(2);
const isHtml = args.includes('--html');
const filtered = args.filter(a => a !== '--html');

if (filtered.length < 3) {
  console.error('Usage: node send-email.js <to> <subject> <body> [--html]');
  process.exit(1);
}

const [to, subject, body] = filtered;

async function main() {
  const transporter = nodemailer.createTransport({
    host: creds.smtp.host,
    port: creds.smtp.port,
    secure: creds.smtp.secure,
    auth: { user: creds.email, pass: creds.password }
  });

  const mailOpts = {
    from: `"Clive" <${creds.email}>`,
    to,
    subject
  };

  if (isHtml) {
    mailOpts.html = body;
  } else {
    mailOpts.text = body;
  }

  const info = await transporter.sendMail(mailOpts);
  console.log(`Sent: ${info.messageId}`);
}

main().catch(err => { console.error(err.message); process.exit(1); });
