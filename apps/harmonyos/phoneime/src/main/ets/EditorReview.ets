import { inputMethodEngine } from '@kit.IMEKit';

const MAX_TEXT_UNITS = 128;
const MAX_SELECTION_INDEX = 4096;
const REVIEW_MS = 20000;
const IDLE = '请点选一项操作。';

interface Target {
  bundleName: string;
  windowId: number;
  mode: inputMethodEngine.SecurityMode;
}

interface PendingEdit {
  id: number;
  generation: number;
  target: Target;
  reviewedAt: number;
  apply: (client: inputMethodEngine.InputClient) => void;
}

export class EditorReview {
  readonly storage: LocalStorage = new LocalStorage();
  private readonly engine: inputMethodEngine.InputMethodAbility;
  private client: inputMethodEngine.InputClient | undefined;
  private visible: boolean = false;
  private generation: number = 0;
  private nextId: number = 0;
  private pending: PendingEdit | undefined;

  constructor(engine: inputMethodEngine.InputMethodAbility) {
    this.engine = engine;
    this.storage.setOrCreate<EditorReview>('review', this);
    this.storage.setOrCreate('reviewId', 0);
    this.storage.setOrCreate('reviewLabel', '');
    this.storage.setOrCreate('status', IDLE);
  }

  bind(client: inputMethodEngine.InputClient | undefined): void {
    this.client = client;
    this.invalidate();
  }

  setVisible(visible: boolean): void {
    this.visible = visible;
    this.invalidate();
  }

  invalidate(): void {
    this.generation++;
    if (this.pending !== undefined) {
      this.clearReview();
      this.status('已取消。输入框或键盘状态已变化。');
    }
  }

  stageInsert(text: string): void {
    if (text.length < 1 || text.length > MAX_TEXT_UNITS) {
      return;
    }
    this.stage(`插入文字「${text}」`, (client) => client.insertTextSync(text));
  }

  stageSelection(start: number, end: number): void {
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > MAX_SELECTION_INDEX) {
      return;
    }
    const range: inputMethodEngine.Range = { start, end };
    this.stage(`选择文字，位置 ${start} 至 ${end}`, (client) => client.selectByRangeSync(range));
  }

  stageMove(direction: inputMethodEngine.Direction): void {
    const labels: string[] = ['上', '下', '左', '右'];
    if (!Number.isInteger(direction) || direction < inputMethodEngine.Direction.CURSOR_UP ||
      direction > inputMethodEngine.Direction.CURSOR_RIGHT) {
      return;
    }
    this.stage(`光标向${labels[direction - 1]}移动一次`, (client) => client.moveCursorSync(direction));
  }

  cancel(id: number): void {
    if (id === this.pending?.id) {
      this.clearReview();
      this.status(`操作 ${id} 已取消。`);
    }
  }

  confirm(id: number): void {
    const edit = this.pending;
    const client = this.client;
    if (edit === undefined || id !== edit.id) {
      return;
    }
    this.clearReview();
    const age = Date.now() - edit.reviewedAt;
    const target = this.currentTarget();
    if (client === undefined || edit.generation !== this.generation || age < 0 || age >= REVIEW_MS ||
      target === undefined || edit.target.bundleName !== target.bundleName || edit.target.windowId !== target.windowId ||
      edit.target.mode !== target.mode) {
      this.status(`操作 ${id} 已取消。输入框已变化或确认已过期。`);
      return;
    }
    try {
      edit.apply(client);
      this.status(`操作 ${id} 已提交给输入框。请查看实际结果。`);
    } catch (_) {
      this.status(`操作 ${id} 结果未知。请先查看输入框。`);
    }
  }

  private stage(label: string, apply: (client: inputMethodEngine.InputClient) => void): void {
    if (this.pending !== undefined) {
      return;
    }
    const target = this.currentTarget();
    if (target === undefined) {
      this.status('请聚焦一个可识别的普通文本输入框。');
      return;
    }
    const id = ++this.nextId;
    this.pending = { id, generation: this.generation, target, reviewedAt: Date.now(), apply };
    this.storage.set('reviewLabel', `${label}\n${target.bundleName} · 窗口 ${target.windowId}`);
    this.storage.set('reviewId', id);
    this.status('请核对输入框和操作。');
  }

  private currentTarget(): Target | undefined {
    if (this.client === undefined || !this.visible) {
      return undefined;
    }
    try {
      const mode = this.engine.getSecurityMode();
      const attr = this.client.getEditorAttributeSync();
      const bundleName = attr.bundleName;
      const windowId = attr.windowId;
      if ((mode !== inputMethodEngine.SecurityMode.BASIC && mode !== inputMethodEngine.SecurityMode.FULL) ||
        attr.inputPattern !== inputMethodEngine.PATTERN_TEXT || bundleName === undefined || bundleName.length === 0 ||
        windowId === undefined || !Number.isInteger(windowId) || windowId <= 0) {
        return undefined;
      }
      return { bundleName, windowId, mode };
    } catch (_) {
      return undefined;
    }
  }

  private clearReview(): void {
    this.pending = undefined;
    this.storage.set('reviewId', 0);
    this.storage.set('reviewLabel', '');
  }

  private status(message: string): void {
    this.storage.set('status', message);
  }
}
