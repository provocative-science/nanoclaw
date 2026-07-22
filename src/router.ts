import { Channel, NewMessage } from './types.js';
import { formatLocalTime } from './timezone.js';

export function escapeXml(s: string): string {
  if (!s) return '';
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Forum / topic routing for outbound replies.
 *
 * Prefer an explicit anchor (e.g. model-escalate message), else the newest
 * trigger match, else the newest message. Use that message's thread_id as-is —
 * including absent/empty for Telegram General (Bot API omits message_thread_id).
 *
 * Do not scan older messages for a non-empty thread_id: a General trigger would
 * otherwise inherit a stale topic from earlier in the batch.
 */
export function replyThreadIdFromBatch(
  messages: NewMessage[],
  options?: {
    anchor?: NewMessage;
    triggerPattern?: RegExp;
  },
): string | undefined {
  let anchor = options?.anchor;
  if (!anchor && options?.triggerPattern) {
    const pattern = options.triggerPattern;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (pattern.test((messages[i].content || '').trim())) {
        anchor = messages[i];
        break;
      }
    }
  }
  anchor ??= messages[messages.length - 1];
  const t = anchor?.thread_id;
  if (t != null && t !== '') return t;
  return undefined;
}

export function formatMessages(
  messages: NewMessage[],
  timezone: string,
): string {
  const lines = messages.map((m) => {
    const displayTime = formatLocalTime(m.timestamp, timezone);
    const replyAttr = m.reply_to_message_id
      ? ` reply_to="${escapeXml(m.reply_to_message_id)}"`
      : '';
    const replySnippet =
      m.reply_to_message_content && m.reply_to_sender_name
        ? `\n  <quoted_message from="${escapeXml(m.reply_to_sender_name)}">${escapeXml(m.reply_to_message_content)}</quoted_message>`
        : '';
    return `<message sender="${escapeXml(m.sender_name)}" time="${escapeXml(displayTime)}"${replyAttr}>${replySnippet}${escapeXml(m.content)}</message>`;
  });

  const header = `<context timezone="${escapeXml(timezone)}" />\n`;

  return `${header}<messages>\n${lines.join('\n')}\n</messages>`;
}

export function stripInternalTags(text: string): string {
  return text.replace(/<internal>[\s\S]*?<\/internal>/g, '').trim();
}

export function formatOutbound(rawText: string): string {
  const text = stripInternalTags(rawText);
  if (!text) return '';
  return text;
}

export function routeOutbound(
  channels: Channel[],
  jid: string,
  text: string,
  threadId?: string,
): Promise<void> {
  const channel = channels.find((c) => c.ownsJid(jid) && c.isConnected());
  if (!channel) throw new Error(`No channel for JID: ${jid}`);
  return channel.sendMessage(jid, text, threadId);
}

export function findChannel(
  channels: Channel[],
  jid: string,
): Channel | undefined {
  return channels.find((c) => c.ownsJid(jid));
}
