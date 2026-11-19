export const CHAT_MAX_FILE_BYTES: number = 32 * 1024 * 1024;
export const CHAT_CHUNK_BYTES: number = 48 * 1024;
export const CHAT_MAX_FILES: number = 4;
export const CHAT_MAX_TEXT_BYTES: number = 16 * 1024;
export const CHAT_MAX_LOCAL_MESSAGES: number = 256;

export interface ChatAttachment {
  attachment_id: string;
  name: string;
  mime: string;
  bytes: number;
}

export interface ChatEvent {
  sequence: number;
  event_id: string;
  kind: string;
  role: string;
  text: string;
  created_at_ms: number | null;
  task_id?: string | null;
  reply_to?: string | null;
  item_id?: string | null;
  state?: string | null;
  attachments: ChatAttachment[];
}

export interface ChatPending {
  event_id: string;
  created_at_ms: number;
  text: string;
  attachments: ChatAttachment[];
  branch_id?: string;
  accepted?: boolean;
}

export interface ChatApproval {
  interaction_id: string;
  task_id: string;
  prompt: string;
}

export interface ChatEvents {
  events: ChatEvent[];
  next_cursor: number;
  approvals: ChatApproval[];
  mode: string;
}

export interface ChatJournal {
  node_id: string;
  session_id: string;
  cursor: number;
  events: ChatEvent[];
  queue: ChatPending[];
  branch_id: string;
  approvals: ChatApproval[];
}

export interface ChatRow {
  event_id: string;
  role: string;
  text: string;
  created_at_ms: number | null;
  attachments: ChatAttachment[];
  source: string;
  sending: boolean;
  progress: string;
}

export interface ChatView {
  rows: ChatRow[];
  approvals: ChatApproval[];
  queued: number;
  attempts: number;
  cursor: number;
  mode: string;
  problem: string;
  queueDetails: string;
  reachable: boolean;
}

export function initialChatView(): ChatView {
  return { rows: [], approvals: [], queued: 0, attempts: 0, cursor: 0, mode: 'connecting', problem: '', queueDetails: '', reachable: false };
}

export function freshChatJournal(nodeId: string, sessionId: string): ChatJournal {
  return { node_id: nodeId, session_id: sessionId, cursor: 0, events: [], queue: [], branch_id: '', approvals: [] };
}

function eventRow(event: ChatEvent): ChatRow {
  return { event_id: event.event_id, role: event.role, text: event.text, created_at_ms: event.created_at_ms,
    attachments: event.attachments, source: event.kind, sending: event.kind === 'delta', progress: '' };
}

export function chatRows(journal: ChatJournal): ChatRow[] {
  const rows: ChatRow[] = [];
  const partials: Map<string, ChatRow> = new Map();
  const originalEvents: Set<string> = new Set();
  const answered: Set<string> = new Set();
  const progress: Map<string, string> = new Map();
  for (const event of journal.events) {
    const group: string = event.reply_to || event.task_id || event.event_id;
    if (event.kind === 'receipt') {
      if (event.state) { progress.set(group, event.state); }
      continue;
    }
    if (event.kind === 'delta') {
      if (!answered.has(group)) { partials.set(group + '|' + (event.item_id || ''), eventRow(event)); }
      continue;
    }
    if (event.kind === 'reply') { answered.add(group); }
    rows.push(eventRow(event));
    originalEvents.add(event.event_id);
  }
  for (const pending of journal.queue) {
    if (!originalEvents.has(pending.event_id)) {
      rows.push({ event_id: pending.event_id, role: 'user', text: pending.text,
        created_at_ms: pending.created_at_ms, attachments: pending.attachments, source: 'local',
        sending: pending.accepted !== true, progress: pending.accepted === true ? 'sent' : '' });
    }
  }
  for (const row of rows) {
    if (row.role === 'user' && progress.has(row.event_id)) { row.progress = progress.get(row.event_id) as string; }
  }
  partials.forEach((row: ChatRow, key: string): void => {
    if (!answered.has(key.slice(0, key.indexOf('|')))) { rows.push(row); }
  });
  return rows.sort((left: ChatRow, right: ChatRow): number => (left.created_at_ms ?? 0) - (right.created_at_ms ?? 0));
}

export function mergeChatEvents(journal: ChatJournal, page: ChatEvents): void {
  if (!Number.isSafeInteger(page.next_cursor) || page.next_cursor < journal.cursor ||
      !Array.isArray(page.events) || !Array.isArray(page.approvals)) {
    throw new Error('chat_invalid_event_page');
  }
  const identities: Set<string> = new Set(journal.events.map((event: ChatEvent): string => event.event_id));
  let sequence: number = journal.cursor;
  for (const event of page.events) {
    if (!Number.isSafeInteger(event.sequence) || event.sequence <= sequence || event.sequence > page.next_cursor ||
        typeof event.event_id !== 'string' || typeof event.text !== 'string' || !Array.isArray(event.attachments) ||
        (event.created_at_ms !== null && !Number.isSafeInteger(event.created_at_ms))) {
      throw new Error('chat_invalid_event');
    }
    sequence = event.sequence;
  }
  for (const event of page.events) {
    if (identities.has(event.event_id)) { continue; }
    if (event.kind === 'delta') {
      journal.events = journal.events.filter((old: ChatEvent): boolean => old.kind !== 'delta' ||
        old.reply_to !== event.reply_to || old.item_id !== event.item_id);
    }
    if (event.kind === 'reply') {
      journal.events = journal.events.filter((old: ChatEvent): boolean => old.kind !== 'delta' || old.reply_to !== event.reply_to);
    }
    journal.events.push(event);
    identities.add(event.event_id);
  }
  journal.queue = journal.queue.filter((pending: ChatPending): boolean => !identities.has(pending.event_id));
  if (journal.events.length > 1000) { journal.events = journal.events.slice(-1000); }
  journal.cursor = page.next_cursor;
  journal.approvals = page.approvals;
}
