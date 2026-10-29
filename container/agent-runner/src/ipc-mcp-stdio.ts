/**
 * Stdio MCP Server for NanoClaw
 * Standalone process that agent teams subagents can inherit.
 * Reads context from environment variables, writes IPC files for the host.
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import fs from 'fs';
import path from 'path';
import { CronExpressionParser } from 'cron-parser';

const IPC_DIR = '/workspace/ipc';
const MESSAGES_DIR = path.join(IPC_DIR, 'messages');
const TASKS_DIR = path.join(IPC_DIR, 'tasks');

// Context from environment variables (set by the agent runner)
const chatJid = process.env.NANOCLAW_CHAT_JID!;
const groupFolder = process.env.NANOCLAW_GROUP_FOLDER!;
const isMain = process.env.NANOCLAW_IS_MAIN === '1';
const replyThreadIdFromHost =
  process.env.NANOCLAW_REPLY_THREAD_ID?.trim() || undefined;
/** Replay evals: nothing leaves the container; writes are captured for scoring. */
const evalMode = process.env.NANOCLAW_EVAL_MODE === '1';
const EVAL_CAPTURE_DIR = '/workspace/group/eval-ipc';
const KNOWLEDGE_ENTRIES_DIR = '/workspace/knowledge/entries';
const INPUT_DIR = path.join(IPC_DIR, 'input');

function writeIpcFile(dir: string, data: object): string {
  if (evalMode) dir = path.join(EVAL_CAPTURE_DIR, path.basename(dir));
  fs.mkdirSync(dir, { recursive: true });

  const filename = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`;
  const filepath = path.join(dir, filename);

  // Atomic write: temp file then rename
  const tempPath = `${filepath}.tmp`;
  fs.writeFileSync(tempPath, JSON.stringify(data, null, 2));
  fs.renameSync(tempPath, filepath);

  return filename;
}

const server = new McpServer({
  name: 'nanoclaw',
  version: '1.0.0',
});

server.tool(
  'send_message',
  "Send a message to the user or group immediately while you're still running. Use this for progress updates or to send multiple messages. You can call this multiple times.",
  {
    text: z.string().describe('The message text to send'),
    sender: z.string().optional().describe('Your role/identity name (e.g. "Researcher"). When set, messages appear from a dedicated bot in Telegram.'),
  },
  async (args) => {
    const data: Record<string, string | undefined> = {
      type: 'message',
      chatJid,
      text: args.text,
      sender: args.sender || undefined,
      groupFolder,
      timestamp: new Date().toISOString(),
    };
    if (replyThreadIdFromHost) {
      data.threadId = replyThreadIdFromHost;
    }

    writeIpcFile(MESSAGES_DIR, data);

    return { content: [{ type: 'text' as const, text: 'Message sent.' }] };
  },
);

server.tool(
  'send_photo',
  'Send an image to Telegram from a file under the container workspace (e.g. /workspace/group/…). The host resolves the path and uploads it; only image extensions (png, jpeg, gif, webp, bmp, tiff) are accepted. Optional caption (Telegram limit applies).',
  {
    workspace_path: z
      .string()
      .describe(
        'Absolute path inside the container, such as /workspace/group/diagram.png',
      ),
    caption: z.string().optional().describe('Optional short caption shown in Telegram'),
  },
  async (args) => {
    const data: Record<string, string | undefined> = {
      type: 'photo',
      chatJid,
      workspacePath: args.workspace_path,
      groupFolder,
      timestamp: new Date().toISOString(),
    };
    if (args.caption?.trim()) {
      data.caption = args.caption.trim();
    }
    if (replyThreadIdFromHost) {
      data.threadId = replyThreadIdFromHost;
    }

    writeIpcFile(MESSAGES_DIR, data);

    return {
      content: [
        { type: 'text' as const, text: 'Photo queued for delivery to Telegram.' },
      ],
    };
  },
);

server.tool(
  'send_file',
  'Send a file as a Telegram document attachment from a file under the container workspace (e.g. /workspace/group/report.md). Use this for markdown, text, CSV, JSON, PDF, logs, or anything that is not a raster image (use send_photo for images). The host resolves the path and uploads it; dotfiles such as .env or .mcp.json are refused. Max 50 MB. Optional caption (Telegram limit applies).',
  {
    workspace_path: z
      .string()
      .describe(
        'Absolute path inside the container, such as /workspace/group/reports/summary.md',
      ),
    caption: z.string().optional().describe('Optional short caption shown with the file in Telegram'),
    filename: z
      .string()
      .optional()
      .describe('Optional display filename (defaults to the basename of workspace_path)'),
  },
  async (args) => {
    const data: Record<string, string | undefined> = {
      type: 'file',
      chatJid,
      workspacePath: args.workspace_path,
      groupFolder,
      timestamp: new Date().toISOString(),
    };
    if (args.caption?.trim()) {
      data.caption = args.caption.trim();
    }
    if (args.filename?.trim()) {
      data.filename = args.filename.trim();
    }
    if (replyThreadIdFromHost) {
      data.threadId = replyThreadIdFromHost;
    }

    writeIpcFile(MESSAGES_DIR, data);

    return {
      content: [
        { type: 'text' as const, text: 'File queued for delivery to Telegram.' },
      ],
    };
  },
);

server.tool(
  'schedule_task',
  `Schedule a recurring or one-time task. The task will run as a full agent with access to all tools. Returns the task ID for future reference. To modify an existing task, use update_task instead.

CONTEXT MODE - Choose based on task type:
\u2022 "group": Task runs in the group's conversation context, with access to chat history. Use for tasks that need context about ongoing discussions, user preferences, or recent interactions.
\u2022 "isolated": Task runs in a fresh session with no conversation history. Use for independent tasks that don't need prior context. When using isolated mode, include all necessary context in the prompt itself.

If unsure which mode to use, you can ask the user. Examples:
- "Remind me about our discussion" \u2192 group (needs conversation context)
- "Check the weather every morning" \u2192 isolated (self-contained task)
- "Follow up on my request" \u2192 group (needs to know what was requested)
- "Generate a daily report" \u2192 isolated (just needs instructions in prompt)

MESSAGING BEHAVIOR - The task agent's output is sent to the user or group. It can also use send_message for immediate delivery, or wrap output in <internal> tags to suppress it. Include guidance in the prompt about whether the agent should:
\u2022 Always send a message (e.g., reminders, daily briefings)
\u2022 Only send a message when there's something to report (e.g., "notify me if...")
\u2022 Never send a message (background maintenance tasks)

SCHEDULE VALUE FORMAT (all times are LOCAL timezone):
\u2022 cron: Standard cron expression (e.g., "*/5 * * * *" for every 5 minutes, "0 9 * * *" for daily at 9am LOCAL time)
\u2022 interval: Milliseconds between runs (e.g., "300000" for 5 minutes, "3600000" for 1 hour)
\u2022 once: Local time WITHOUT "Z" suffix (e.g., "2026-02-01T15:30:00"). Do NOT use UTC/Z suffix.`,
  {
    prompt: z.string().describe('What the agent should do when the task runs. For isolated mode, include all necessary context here.'),
    schedule_type: z.enum(['cron', 'interval', 'once']).describe('cron=recurring at specific times, interval=recurring every N ms, once=run once at specific time'),
    schedule_value: z.string().describe('cron: "*/5 * * * *" | interval: milliseconds like "300000" | once: local timestamp like "2026-02-01T15:30:00" (no Z suffix!)'),
    context_mode: z.enum(['group', 'isolated']).default('group').describe('group=runs with chat history and memory, isolated=fresh session (include context in prompt)'),
    target_group_jid: z.string().optional().describe('(Main group only) JID of the group to schedule the task for. Defaults to the current group.'),
    script: z.string().optional().describe('Optional bash script to run before waking the agent. Script must output JSON on the last line of stdout: { "wakeAgent": boolean, "data"?: any }. If wakeAgent is false, the agent is not called. Test your script with bash -c "..." before scheduling.'),
    thread_id: z.string().optional().describe('Optional Telegram forum topic id (message_thread_id). When set, the task agent replies and send_message go to that topic.'),
    model: z.string().optional().describe('Optional Claude/light model: sonnet | opus | haiku | qwen, or full IDs. qwen is light-path only (no tools). Omit for host default (sonnet).'),
  },
  async (args) => {
    // Validate schedule_value before writing IPC
    if (args.schedule_type === 'cron') {
      try {
        CronExpressionParser.parse(args.schedule_value);
      } catch {
        return {
          content: [{ type: 'text' as const, text: `Invalid cron: "${args.schedule_value}". Use format like "0 9 * * *" (daily 9am) or "*/5 * * * *" (every 5 min).` }],
          isError: true,
        };
      }
    } else if (args.schedule_type === 'interval') {
      const ms = parseInt(args.schedule_value, 10);
      if (isNaN(ms) || ms <= 0) {
        return {
          content: [{ type: 'text' as const, text: `Invalid interval: "${args.schedule_value}". Must be positive milliseconds (e.g., "300000" for 5 min).` }],
          isError: true,
        };
      }
    } else if (args.schedule_type === 'once') {
      if (/[Zz]$/.test(args.schedule_value) || /[+-]\d{2}:\d{2}$/.test(args.schedule_value)) {
        return {
          content: [{ type: 'text' as const, text: `Timestamp must be local time without timezone suffix. Got "${args.schedule_value}" — use format like "2026-02-01T15:30:00".` }],
          isError: true,
        };
      }
      const date = new Date(args.schedule_value);
      if (isNaN(date.getTime())) {
        return {
          content: [{ type: 'text' as const, text: `Invalid timestamp: "${args.schedule_value}". Use local time format like "2026-02-01T15:30:00".` }],
          isError: true,
        };
      }
    }

    // Non-main groups can only schedule for themselves
    const targetJid = isMain && args.target_group_jid ? args.target_group_jid : chatJid;

    const taskId = `task-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

    const data = {
      type: 'schedule_task',
      taskId,
      prompt: args.prompt,
      script: args.script || undefined,
      schedule_type: args.schedule_type,
      schedule_value: args.schedule_value,
      context_mode: args.context_mode || 'group',
      targetJid,
      threadId: args.thread_id || undefined,
      model: args.model || undefined,
      createdBy: groupFolder,
      timestamp: new Date().toISOString(),
    };

    writeIpcFile(TASKS_DIR, data);

    return {
      content: [{ type: 'text' as const, text: `Task ${taskId} scheduled: ${args.schedule_type} - ${args.schedule_value}` }],
    };
  },
);

server.tool(
  'list_tasks',
  "List all scheduled tasks. From main: shows all tasks. From other groups: shows only that group's tasks.",
  {},
  async () => {
    const tasksFile = path.join(IPC_DIR, 'current_tasks.json');

    try {
      if (!fs.existsSync(tasksFile)) {
        return { content: [{ type: 'text' as const, text: 'No scheduled tasks found.' }] };
      }

      const allTasks = JSON.parse(fs.readFileSync(tasksFile, 'utf-8'));

      const tasks = isMain
        ? allTasks
        : allTasks.filter((t: { groupFolder: string }) => t.groupFolder === groupFolder);

      if (tasks.length === 0) {
        return { content: [{ type: 'text' as const, text: 'No scheduled tasks found.' }] };
      }

      const formatted = tasks
        .map(
          (t: { id: string; prompt: string; schedule_type: string; schedule_value: string; status: string; next_run: string }) =>
            `- [${t.id}] ${t.prompt.slice(0, 50)}... (${t.schedule_type}: ${t.schedule_value}) - ${t.status}, next: ${t.next_run || 'N/A'}`,
        )
        .join('\n');

      return { content: [{ type: 'text' as const, text: `Scheduled tasks:\n${formatted}` }] };
    } catch (err) {
      return {
        content: [{ type: 'text' as const, text: `Error reading tasks: ${err instanceof Error ? err.message : String(err)}` }],
      };
    }
  },
);

server.tool(
  'pause_task',
  'Pause a scheduled task. It will not run until resumed.',
  { task_id: z.string().describe('The task ID to pause') },
  async (args) => {
    const data = {
      type: 'pause_task',
      taskId: args.task_id,
      groupFolder,
      isMain,
      timestamp: new Date().toISOString(),
    };

    writeIpcFile(TASKS_DIR, data);

    return { content: [{ type: 'text' as const, text: `Task ${args.task_id} pause requested.` }] };
  },
);

server.tool(
  'resume_task',
  'Resume a paused task.',
  { task_id: z.string().describe('The task ID to resume') },
  async (args) => {
    const data = {
      type: 'resume_task',
      taskId: args.task_id,
      groupFolder,
      isMain,
      timestamp: new Date().toISOString(),
    };

    writeIpcFile(TASKS_DIR, data);

    return { content: [{ type: 'text' as const, text: `Task ${args.task_id} resume requested.` }] };
  },
);

server.tool(
  'cancel_task',
  'Cancel and delete a scheduled task.',
  { task_id: z.string().describe('The task ID to cancel') },
  async (args) => {
    const data = {
      type: 'cancel_task',
      taskId: args.task_id,
      groupFolder,
      isMain,
      timestamp: new Date().toISOString(),
    };

    writeIpcFile(TASKS_DIR, data);

    return { content: [{ type: 'text' as const, text: `Task ${args.task_id} cancellation requested.` }] };
  },
);

server.tool(
  'update_task',
  'Update an existing scheduled task. Only provided fields are changed; omitted fields stay the same.',
  {
    task_id: z.string().describe('The task ID to update'),
    prompt: z.string().optional().describe('New prompt for the task'),
    schedule_type: z.enum(['cron', 'interval', 'once']).optional().describe('New schedule type'),
    schedule_value: z.string().optional().describe('New schedule value (see schedule_task for format)'),
    script: z.string().optional().describe('New script for the task. Set to empty string to remove the script.'),
    model: z.string().optional().describe('New model (sonnet | opus | haiku or full ID). Set to empty string to clear and use host default.'),
  },
  async (args) => {
    // Validate schedule_value if provided
    if (args.schedule_type === 'cron' || (!args.schedule_type && args.schedule_value)) {
      if (args.schedule_value) {
        try {
          CronExpressionParser.parse(args.schedule_value);
        } catch {
          return {
            content: [{ type: 'text' as const, text: `Invalid cron: "${args.schedule_value}".` }],
            isError: true,
          };
        }
      }
    }
    if (args.schedule_type === 'interval' && args.schedule_value) {
      const ms = parseInt(args.schedule_value, 10);
      if (isNaN(ms) || ms <= 0) {
        return {
          content: [{ type: 'text' as const, text: `Invalid interval: "${args.schedule_value}".` }],
          isError: true,
        };
      }
    }

    const data: Record<string, string | undefined> = {
      type: 'update_task',
      taskId: args.task_id,
      groupFolder,
      isMain: String(isMain),
      timestamp: new Date().toISOString(),
    };
    if (args.prompt !== undefined) data.prompt = args.prompt;
    if (args.script !== undefined) data.script = args.script;
    if (args.schedule_type !== undefined) data.schedule_type = args.schedule_type;
    if (args.schedule_value !== undefined) data.schedule_value = args.schedule_value;
    if (args.model !== undefined) data.model = args.model;

    writeIpcFile(TASKS_DIR, data);

    return { content: [{ type: 'text' as const, text: `Task ${args.task_id} update requested.` }] };
  },
);

server.tool(
  'register_group',
  `Register a new chat/group so the agent can respond to messages there. Main group only.

Use available_groups.json to find the JID for a group. The folder name must be channel-prefixed: "{channel}_{group-name}" (e.g., "whatsapp_family-chat", "telegram_dev-team", "discord_general"). Use lowercase with hyphens for the group name part.`,
  {
    jid: z.string().describe('The chat JID (e.g., "120363336345536173@g.us", "tg:-1001234567890", "dc:1234567890123456")'),
    name: z.string().describe('Display name for the group'),
    folder: z.string().describe('Channel-prefixed folder name (e.g., "whatsapp_family-chat", "telegram_dev-team")'),
    trigger: z.string().describe('Trigger word (e.g., "@Andy")'),
  },
  async (args) => {
    if (!isMain) {
      return {
        content: [{ type: 'text' as const, text: 'Only the main group can register new groups.' }],
        isError: true,
      };
    }

    const data = {
      type: 'register_group',
      jid: args.jid,
      name: args.name,
      folder: args.folder,
      trigger: args.trigger,
      timestamp: new Date().toISOString(),
    };

    writeIpcFile(TASKS_DIR, data);

    return {
      content: [{ type: 'text' as const, text: `Group "${args.name}" registered. It will start receiving messages immediately.` }],
    };
  },
);

// ---------------------------------------------------------------------------
// Knowledge store (read directly from the read-only mount; writes via host IPC)
// ---------------------------------------------------------------------------

interface KbEntry {
  id: string;
  kind: string;
  title: string;
  tags: string[];
  systems?: string[];
  state: string;
  confidence: string;
  updated?: string;
  expires?: string;
  last_check?: { at?: string; result?: 'holds' | 'no_longer_holds' | 'error'; detail?: string };
  body: string;
  raw: string;
}

function parseKbEntry(raw: string): KbEntry | null {
  const match = raw.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/);
  if (!match) return null;
  const fields: Record<string, unknown> = {};
  for (const line of match[1].split('\n')) {
    const idx = line.indexOf(':');
    if (idx < 0) continue;
    const value = line.slice(idx + 1).trim();
    try {
      fields[line.slice(0, idx).trim()] = JSON.parse(value);
    } catch {
      fields[line.slice(0, idx).trim()] = value;
    }
  }
  return { ...(fields as Omit<KbEntry, 'body' | 'raw'>), tags: (fields.tags as string[]) || [], body: match[2].trim(), raw };
}

function loadKbEntries(): KbEntry[] {
  if (!fs.existsSync(KNOWLEDGE_ENTRIES_DIR)) return [];
  const out: KbEntry[] = [];
  for (const file of fs.readdirSync(KNOWLEDGE_ENTRIES_DIR)) {
    if (!file.endsWith('.md')) continue;
    const entry = parseKbEntry(fs.readFileSync(path.join(KNOWLEDGE_ENTRIES_DIR, file), 'utf-8'));
    if (entry) out.push(entry);
  }
  return out;
}

const tokens = (text: string) =>
  new Set((text.toLowerCase().match(/[a-z0-9_]{3,}/g) || []).filter((t) => !['the', 'and', 'for', 'with', 'was', 'are'].includes(t)));

function kbSummary(e: KbEntry): string {
  const extra = [
    e.expires ? `expires ${e.expires.slice(0, 10)}` : '',
    e.last_check?.at ? `check ${e.last_check.result ?? 'error'} @${e.last_check.at.slice(0, 16)}` : '',
  ].filter(Boolean).join(', ');
  const body = e.body.replace(/\s+/g, ' ').slice(0, 220);
  return `- ${e.id} [${e.kind}/${e.state}/${e.confidence}] ${e.title}${extra ? ` (${extra})` : ''}\n  tags: ${e.tags.join(', ')}\n  ${body}${e.body.length > 220 ? '…' : ''}`;
}

async function kbWrite(payload: Record<string, unknown>): Promise<{ text: string; isError: boolean }> {
  if (evalMode) return { text: 'Eval mode: knowledge writes are disabled.', isError: false };
  const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  writeIpcFile(TASKS_DIR, { type: 'kb_write', requestId, ...payload, timestamp: new Date().toISOString() });
  const resultPath = path.join(INPUT_DIR, `kb_result_${requestId}.json`);
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 500));
    if (!fs.existsSync(resultPath)) continue;
    try {
      const result = JSON.parse(fs.readFileSync(resultPath, 'utf-8')) as { ok: boolean; message: string };
      fs.unlinkSync(resultPath);
      return { text: result.message, isError: !result.ok };
    } catch {
      /* partially written; retry */
    }
  }
  return { text: 'Knowledge write queued but no confirmation within 30s; check with kb_search before retrying.', isError: true };
}

const checkSchema = z
  .object({
    type: z.enum(['stream_fresh', 's3_sql', 'prom']),
    host: z.string().optional().describe('stream_fresh: telemetry host, e.g. airbed-host or bop-host'),
    stream: z.string().optional().describe('stream_fresh: telemetry (default) or console'),
    max_age_hours: z.number().optional().describe('stream_fresh: fresh when newest object is younger than this'),
    sql: z.string().optional().describe('s3_sql: DuckDB query over the plant archive returning one scalar (same SQL as mcp__telemetry__query_sql)'),
    query: z.string().optional().describe('prom: PromQL instant query returning one scalar'),
    op: z.enum(['<', '<=', '>', '>=', '==', '!=']).optional(),
    value: z.union([z.number(), z.string(), z.boolean()]).optional(),
    holds_when: z.boolean().optional().describe('The status is true while the check result equals this (default true)'),
  })
  .describe('Machine check the host runs hourly: while it holds the status stays, when it stops holding the status auto-resolves.');

server.tool(
  'kb_search',
  'Search the shared knowledge store (rules, plant status, lessons, corrections, facts, notes) before investigating or answering. Active entries only unless include_inactive. Returns id, kind/state/confidence, title and a body excerpt; use kb_get for the full entry.',
  {
    query: z.string().optional().describe('Free text; matched against title, tags and body'),
    kind: z.enum(['rule', 'status', 'lesson', 'correction', 'fact', 'note']).optional(),
    tags: z.array(z.string()).optional(),
    system: z.string().optional().describe('airbed | container | liquefaction | …'),
    include_inactive: z.boolean().optional(),
    limit: z.number().int().min(1).max(40).optional(),
  },
  async (args) => {
    const q = tokens(args.query || '');
    const wantTags = (args.tags || []).map((t) => t.toLowerCase());
    const scored = loadKbEntries()
      .filter((e) => args.include_inactive || e.state === 'active' || e.state === 'needs_review')
      .filter((e) => !args.kind || e.kind === args.kind)
      .filter((e) => !args.system || (e.systems || []).includes(args.system.toLowerCase()) || e.tags.includes(args.system.toLowerCase()))
      .filter((e) => wantTags.every((t) => e.tags.includes(t)))
      .map((e) => {
        if (q.size === 0) return { e, score: 1 };
        const title = tokens(`${e.title} ${e.tags.join(' ')}`);
        const body = tokens(e.body);
        let score = 0;
        for (const t of q) score += (title.has(t) ? 3 : 0) + (body.has(t) ? 1 : 0);
        return { e, score };
      })
      .filter((s) => s.score > 0)
      .sort((a, b) => b.score - a.score || String(b.e.updated).localeCompare(String(a.e.updated)))
      .slice(0, args.limit ?? 15);
    if (scored.length === 0) return { content: [{ type: 'text' as const, text: 'No matching knowledge entries.' }] };
    return { content: [{ type: 'text' as const, text: scored.map((s) => kbSummary(s.e)).join('\n') }] };
  },
);

server.tool(
  'kb_get',
  'Read one knowledge entry in full (frontmatter + body). History: the host keeps every change in git.',
  { id: z.string().describe('Entry id, e.g. k-20261029-a1b2c3') },
  async (args) => {
    if (!/^k-[\w-]+$/.test(args.id)) return { content: [{ type: 'text' as const, text: 'Invalid id.' }], isError: true };
    const p = path.join(KNOWLEDGE_ENTRIES_DIR, `${args.id}.md`);
    if (!fs.existsSync(p)) return { content: [{ type: 'text' as const, text: `No entry ${args.id}.` }], isError: true };
    return { content: [{ type: 'text' as const, text: fs.readFileSync(p, 'utf-8') }] };
  },
);

const entryFields = {
  kind: z.enum(['rule', 'status', 'lesson', 'correction', 'fact', 'note']).describe('rule = standing instruction (main chat only); status = time-bound plant state (must expire); lesson = diagnostic lesson; correction = something an agent got wrong; fact = stable reference; note = anything else'),
  title: z.string().describe('One line, 5-160 chars, specific (what + where)'),
  body: z.string().describe('Markdown. For lessons/corrections: what happened, what is true, how to check next time (signal names + query).'),
  tags: z.array(z.string()).min(1).describe('Lowercase topic tags, e.g. ["extraction-bag", "watchdog", "airbed"]'),
  systems: z.array(z.string()).optional().describe('airbed | container | liquefaction | infra'),
  source: z.string().describe('Where this came from: chat message, alert id, tool call + timestamp'),
  confidence: z.enum(['confirmed', 'likely', 'unverified']).optional().describe('Only the main chat can mark confirmed'),
  evidence: z.array(z.string()).optional().describe('Queries/values that support it'),
  ttl_days: z.number().optional().describe('status: days until expiry (default 7, max 90)'),
  expires: z.string().optional().describe('status: ISO expiry instead of ttl_days'),
  check: checkSchema.optional(),
};

server.tool(
  'kb_add',
  'Add a knowledge entry for other agents and future runs. Use for durable lessons and corrections from operators, and for plant status that will matter for days (give status a check when a query can tell whether it still holds). Do not add one-off chat details. Search first; if an entry already covers it, use kb_supersede.',
  entryFields,
  async (args) => {
    const r = await kbWrite({ op: 'add', entry: args });
    return { content: [{ type: 'text' as const, text: r.text }], isError: r.isError };
  },
);

server.tool(
  'kb_supersede',
  'Replace an existing entry with a corrected/updated one (the old one is kept as superseded). If the old entry is confirmed and you are not the main chat, the new one is queued for Spark\'s review as a contradiction instead.',
  {
    id: z.string().describe('Entry being replaced'),
    reason: z.string().describe('Why it changed (evidence)'),
    ...entryFields,
    kind: entryFields.kind.optional(),
    tags: entryFields.tags.optional(),
  },
  async (args) => {
    const { id, reason, ...entry } = args;
    const r = await kbWrite({ op: 'supersede', id, reason, entry });
    return { content: [{ type: 'text' as const, text: r.text }], isError: r.isError };
  },
);

server.tool(
  'kb_resolve',
  'Close or refresh an entry: resolved (status no longer true), retracted (was wrong), still_true (refresh a status expiry). Outside the main chat you may only change your own unconfirmed entries; anything else is queued for Spark\'s review.',
  {
    id: z.string(),
    outcome: z.enum(['resolved', 'retracted', 'still_true']),
    reason: z.string().describe('Evidence for the change'),
  },
  async (args) => {
    const r = await kbWrite({ op: 'resolve', id: args.id, outcome: args.outcome, reason: args.reason });
    return { content: [{ type: 'text' as const, text: r.text }], isError: r.isError };
  },
);

// Start the stdio transport
const transport = new StdioServerTransport();
await server.connect(transport);
