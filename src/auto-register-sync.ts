import { AUTO_REGISTER_SYNC_INTERVAL_MS } from './config.js';
import { runAutoRegisterAllowlistSync } from './auto-register.js';
import { setRouterState } from './db.js';
import { logger } from './logger.js';

export const AUTO_REGISTER_SYNC_STATE_KEY = 'auto_register_allowlist_last_sync';

function syncAllowlistFromGroups(): void {
  try {
    const result = runAutoRegisterAllowlistSync();
    if (result.chats.length === 0) return;

    if (!result.changed) {
      logger.debug(
        { total: result.total, chats: result.chats },
        'Auto-register allowlist sync: no new senders',
      );
    }

    setRouterState(AUTO_REGISTER_SYNC_STATE_KEY, new Date().toISOString());
  } catch (err) {
    logger.error({ err }, 'Auto-register allowlist sync failed');
  }
}

/** Sync Provocative (and other configured) group senders into allowedSenders daily. */
export function startAutoRegisterAllowlistSyncLoop(): void {
  syncAllowlistFromGroups();
  setInterval(syncAllowlistFromGroups, AUTO_REGISTER_SYNC_INTERVAL_MS);
}
