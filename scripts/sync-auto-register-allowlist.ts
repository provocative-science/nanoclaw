#!/usr/bin/env tsx
/**
 * Merge senders from configured group chats into auto-register-allowlist.json.
 * Runs automatically once per day inside nanoclaw; use this for manual sync.
 */
import { initDatabase } from '../src/db.js';
import { runAutoRegisterAllowlistSync } from '../src/auto-register.js';

initDatabase();

const result = runAutoRegisterAllowlistSync();

if (result.chats.length === 0) {
  console.log('No syncFromChats configured in auto-register-allowlist.json');
  process.exit(0);
}

if (result.changed) {
  console.log(
    `Added ${result.added.length} sender(s): ${result.added.join(', ')}`,
  );
  console.log(`Total allowedSenders: ${result.total}`);
} else {
  console.log(
    `No new senders (${result.total} total) from ${result.chats.join(', ')}`,
  );
}
