/**
 * Allowlisted model aliases and full IDs for NanoClaw agent runs.
 * Claude models use the container SDK path; light models use host OpenAI-compat.
 */

export const MODEL_ALIASES: Record<string, string> = {
  sonnet: 'claude-sonnet-4-6',
  opus: 'claude-opus-4-6',
  haiku: 'claude-haiku-4-5',
  qwen: 'qwen3.6-35b',
};

/** Full IDs that may be passed explicitly (same set as alias targets). */
export const MODEL_FULL_IDS = new Set(Object.values(MODEL_ALIASES));

/** Models that run on the host OpenAI-compatible light path (no tools/MCP). */
export const LIGHT_MODEL_IDS = new Set<string>([MODEL_ALIASES.qwen]);

const BUILTIN_ALLOWLIST = new Set([
  ...Object.keys(MODEL_ALIASES),
  ...MODEL_FULL_IDS,
]);

function extraAllowlist(): Set<string> {
  const raw = process.env.NANOCLAW_MODEL_ALLOWLIST;
  if (!raw?.trim()) return new Set();
  return new Set(
    raw
      .split(',')
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean),
  );
}

export function getModelAllowlist(): Set<string> {
  const set = new Set(BUILTIN_ALLOWLIST);
  for (const id of extraAllowlist()) set.add(id);
  return set;
}

export function isLightModel(model: string): boolean {
  return LIGHT_MODEL_IDS.has(model);
}

/**
 * Resolve alias or full ID to a concrete model string, or null if not allowed.
 * Aliases are lowercased; full IDs are matched case-insensitively against the allowlist.
 */
export function normalizeModel(raw: string | null | undefined): string | null {
  if (raw == null) return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;

  const lower = trimmed.toLowerCase();
  const allow = getModelAllowlist();

  if (MODEL_ALIASES[lower]) {
    return MODEL_ALIASES[lower];
  }

  // Full ID: allow exact allowlist hit (case-insensitive)
  for (const id of allow) {
    if (id.toLowerCase() === lower) {
      // Prefer canonical casing from builtin full IDs / alias values
      for (const canonical of MODEL_FULL_IDS) {
        if (canonical.toLowerCase() === lower) return canonical;
      }
      return id;
    }
  }

  return null;
}

/** Alias or full ID list for user-facing error messages. */
export function modelAllowlistHint(): string {
  return 'sonnet | opus | haiku | qwen (or full IDs: claude-sonnet-4-6, claude-opus-4-6, claude-haiku-4-5, qwen3.6-35b)';
}

/**
 * Default model for agent runs when none is specified.
 * Env may be an alias or full ID; invalid values fall back to sonnet.
 * Light models are never the default (requires tools for normal ops).
 */
export function resolveDefaultModel(): string {
  const fromEnv = normalizeModel(process.env.NANOCLAW_DEFAULT_MODEL);
  if (fromEnv && !isLightModel(fromEnv)) return fromEnv;
  return MODEL_ALIASES.sonnet;
}

/** Concrete default at module load (re-read via resolveDefaultModel in hot paths if needed). */
export const DEFAULT_MODEL = resolveDefaultModel();
