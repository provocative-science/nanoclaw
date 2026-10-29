/**
 * Decide whether a chat batch is a plant investigation (strongest model,
 * full briefing, skeptic verifier) or ordinary chat (default model).
 */

export type RunTier = 'investigation' | 'chat';

const DIAGNOSTIC =
  /\b(why|what(?:'s| is| was)? (?:going on|happen(?:ed|ing)|wrong|up with)|diagnos\w*|troubleshoot\w*|root cause|investigat\w*|explain|figure out|look into|dig into|debug\w*|what caused|is (?:it|the \w+) (?:ok|okay|running|locked|down|up)|status (?:of|on)|check (?:on|the|if|whether)|how come)\b/i;

const PLANT =
  /\b(lock(?:ed|out)?|watchdog|interlock|recovery|alarm|alert|trip(?:ped)?|fault\w*|e-?stop|pressure|psi|vacuum|valve|pump|compressor|blower|vfd|airbed|air ?bed|container|liquefaction|liq|tower|tank|telemetry|sensor|temp(?:erature)?|bag|lockhopper|elevator|steam|chiller|sorbent|co3ntrol|supervisor|modbus|riser|backplane|plant|bop|dac|filter)\b/i;

const STRONG =
  /\b(locked|lockout|watchdog|interlock|tripped|fault(?:ed)?|e-?stop|supervisor lock|recovery (?:failed|loop))\b/i;

/** Classify the latest user text of a batch. */
export function classifyIntent(text: string): RunTier {
  const t = (text || '').replace(/@\w[\w_]*/g, ' ');
  if (!t.trim()) return 'chat';
  if (DIAGNOSTIC.test(t) && PLANT.test(t)) return 'investigation';
  if (STRONG.test(t) && /\?/.test(t)) return 'investigation';
  return 'chat';
}
