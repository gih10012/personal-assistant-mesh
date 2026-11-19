import { CHAT_MAX_FILES, CHAT_MAX_TEXT_BYTES, ChatApproval, ChatAttachment, ChatRow, ChatView,
  initialChatView } from '../chat/ChatState';

const INK: string = '#172923';
const SECONDARY: string = '#536760';
const CANVAS: string = '#F4F7F3';
const ACCENT: string = '#185C4A';
const DIVIDER: string = '#DBE4DC';

@Component
export struct ChatHome {
  @Prop @Watch('onConversationChanged') view: ChatView = initialChatView();
  @Prop foreground: boolean = false;
  @Prop ready: boolean = false;
  @Prop notice: string = '';
  @Prop picking: boolean = false;
  @Prop saving: boolean = false;
  @Prop deciding: boolean = false;
  @Prop attachments: ChatAttachment[] = [];
  @Prop draft: string = '';
  onDraft: (text: string) => void = (_: string): void => {};
  onSettings: () => void = (): void => {};
  onRetry: () => void = (): void => {};
  onSend: () => void = (): void => {};
  onPick: () => void = (): void => {};
  onDiscard: (attachment: ChatAttachment) => void = (_: ChatAttachment): void => {};
  onExport: (attachment: ChatAttachment) => void = (_: ChatAttachment): void => {};
  onDecide: (approval: ChatApproval, decision: string) => void = (_: ChatApproval, __: string): void => {};
  private scroller: Scroller = new Scroller();
  private following: boolean = true;
  private scrollTimer: number = -1;

  aboutToAppear(): void { this.onConversationChanged(); }
  aboutToDisappear(): void {
    if (this.scrollTimer !== -1) { clearTimeout(this.scrollTimer); this.scrollTimer = -1; }
  }

  onConversationChanged(): void {
    if (!this.following) { return; }
    if (this.scrollTimer !== -1) { clearTimeout(this.scrollTimer); }
    this.scrollTimer = setTimeout((): void => {
      this.scrollTimer = -1;
      if (this.following && this.view.rows.length > 0) { this.scroller.scrollEdge(Edge.Bottom); }
    }, 60);
  }

  private statusLabel(): string {
    if (this.view.mode === 'unconfigured') { return '在设置中导入设备配置后连接'; }
    if (this.view.mode === 'paused') { return '已暂停'; }
    if (this.view.mode === 'connecting') { return '正在连接 AMITA'; }
    if (!this.view.reachable) { return '离线 · 消息保存在本机'; }
    if (this.view.mode === 'independent') { return '独立对话 · 恢复连接后同步'; }
    return '已连接 · 与 Claw 共用对话';
  }

  private progress(state: string): string {
    switch (state) {
      case 'queued':
      case 'ready':
      case 'pending': return '等待处理';
      case 'active':
      case 'running':
      case 'in_progress': return '正在处理';
      case 'waiting':
      case 'blocked':
      case 'approval_required': return '等待确认';
      case 'done':
      case 'completed': return '已完成';
      case 'failed': return '处理失败';
      case 'cancelled': return '已取消';
      case 'sent': return '已发送';
      default: return '';
    }
  }

  private fileSize(bytes: number): string {
    return bytes < 1024 ? bytes + ' B' : bytes < 1024 * 1024 ?
      (bytes / 1024).toFixed(1) + ' KB' : (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  private timestamp(value: number | null): string {
    if (value === null) { return '时间未知'; }
    const time: Date = new Date(value);
    const today: boolean = time.toDateString() === new Date().toDateString();
    return today ? time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) :
      time.toLocaleDateString() + ' ' + time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  @Builder
  private header() {
    Row({ space: 12 }) {
      Column({ space: 3 }) {
        Text('AMITA').fontSize(26).fontWeight(FontWeight.Bold).fontColor(INK)
        Text(this.statusLabel()).fontSize(12).fontColor(SECONDARY).maxLines(2)
      }.alignItems(HorizontalAlign.Start).layoutWeight(1)
      Button('设置').fontSize(14).height(40).fontColor(ACCENT).backgroundColor('#E5EEE7')
        .onClick((): void => { this.onSettings(); })
    }.width('100%').padding({ left: 20, right: 18, top: 15, bottom: 12 })
  }

  @Builder
  private message(row: ChatRow) {
    Row() {
      Column({ space: 7 }) {
        if (row.text.length > 0) {
          Text(row.text).fontSize(16).lineHeight(24).fontColor(INK).copyOption(CopyOptions.LocalDevice)
        }
        ForEach(row.attachments, (attachment: ChatAttachment) => {
          Button(attachment.name + ' · ' + this.fileSize(attachment.bytes), { type: ButtonType.Normal })
            .fontSize(13).fontColor(ACCENT).backgroundColor('#E7EFE8').borderRadius(9)
            .enabled(!this.saving && this.foreground).height(40).onClick((): void => { this.onExport(attachment); })
        }, (attachment: ChatAttachment): string => attachment.attachment_id)
        Row({ space: 7 }) {
          Text(this.timestamp(row.created_at_ms)).fontSize(11).fontColor(SECONDARY)
          if (row.sending) {
            Text(row.role === 'user' ? '待发送' : '生成中').fontSize(11).fontColor(ACCENT)
          } else if (this.progress(row.progress) !== '') {
            Text(this.progress(row.progress)).fontSize(11).fontColor(ACCENT)
          }
        }
      }.alignItems(HorizontalAlign.Start).constraintSize({ maxWidth: '91%' })
        .padding({ left: 14, right: 14, top: 10, bottom: 8 }).borderRadius(16)
        .backgroundColor(row.role === 'user' ? '#D9ECE0' : row.role === 'system' ? '#E9EFE7' : '#FFFFFF')
    }.justifyContent(row.role === 'user' ? FlexAlign.End : FlexAlign.Start).width('100%')
  }

  @Builder
  private approvalCard(approval: ChatApproval) {
    Column({ space: 9 }) {
      Text('需要确认').fontWeight(FontWeight.Bold).fontSize(14).fontColor(INK)
      Text(approval.prompt).fontSize(14).fontColor(INK).copyOption(CopyOptions.LocalDevice)
      Row({ space: 9 }) {
        Button('拒绝').fontSize(13).height(38).backgroundColor('#E6EDE6').fontColor(INK)
          .enabled(this.foreground && this.view.reachable && !this.deciding)
          .onClick((): void => { this.onDecide(approval, 'decline'); })
        Button('允许').fontSize(13).height(38).backgroundColor(ACCENT)
          .enabled(this.foreground && this.view.reachable && !this.deciding)
          .onClick((): void => { this.onDecide(approval, 'accept'); })
      }
    }.alignItems(HorizontalAlign.Start).width('100%').padding(13).backgroundColor('#FFF5D8').borderRadius(13)
  }

  @Builder
  private composer() {
    Column({ space: 8 }) {
      if (this.view.queued > 0 || this.notice !== '') {
        Row({ space: 8 }) {
          Text(this.notice !== '' ? this.notice : this.view.queued + ' 条消息待发送')
            .fontSize(12).fontColor(SECONDARY).layoutWeight(1)
          if (this.view.queued > 0 && !this.view.reachable) {
            Button('重连').fontSize(12).height(31).fontColor(ACCENT).backgroundColor('#E5EEE7')
              .onClick((): void => { this.onRetry(); })
          }
        }.width('100%')
      }
      if (this.attachments.length > 0) {
        Scroll() {
          Row({ space: 7 }) {
            ForEach(this.attachments, (attachment: ChatAttachment) => {
              Button(attachment.name + ' ×').fontSize(12).height(33).fontColor(INK)
                .backgroundColor('#E5EEE7').constraintSize({ maxWidth: 180 })
                .accessibilityText('移除附件 ' + attachment.name)
                .onClick((): void => { this.onDiscard(attachment); })
            }, (attachment: ChatAttachment): string => attachment.attachment_id)
          }
        }.scrollable(ScrollDirection.Horizontal).scrollBar(BarState.Off).width('100%')
      }
      Row({ space: 8 }) {
        Button('+').fontSize(24).fontWeight(FontWeight.Medium).fontColor(ACCENT).width(42).height(42)
          .backgroundColor('#E5EEE7').accessibilityText('添加附件')
          .enabled(this.ready && this.foreground && !this.picking && this.attachments.length < CHAT_MAX_FILES)
          .onClick((): void => { this.onPick(); })
        TextArea({ text: this.draft, placeholder: '发消息给 AMITA' }).layoutWeight(1)
          .fontSize(15).fontColor(INK).placeholderColor(SECONDARY).backgroundColor('#F0F5EF')
          .constraintSize({ minHeight: 43, maxHeight: 115 }).borderRadius(18).maxLength(CHAT_MAX_TEXT_BYTES)
          .onChange((value: string): void => { this.onDraft(value); })
        Button('发送').fontSize(14).backgroundColor(ACCENT).height(42)
          .enabled(this.ready && this.foreground && (this.draft.trim().length > 0 || this.attachments.length > 0))
          .onClick((): void => {
            this.following = true;
            this.onSend();
          })
      }.width('100%').alignItems(VerticalAlign.Bottom)
    }.width('100%').padding({ left: 13, right: 13, top: 11, bottom: 14 })
      .border({ width: { top: 1 }, color: DIVIDER }).backgroundColor('#FFFFFF')
  }

  build() {
    Column() {
      this.header()
      if (this.view.rows.length === 0 && this.view.approvals.length === 0) {
        Column({ space: 9 }) {
          Text('有什么需要帮忙？').fontSize(22).fontWeight(FontWeight.Medium).fontColor(INK)
          if (this.view.mode === 'unconfigured') {
            Button('配置设备').fontColor(ACCENT).backgroundColor('#E5EEE7').height(42)
              .onClick((): void => { this.onSettings(); })
          }
        }.width('100%').layoutWeight(1).justifyContent(FlexAlign.Center).padding(22)
      } else {
        List({ space: 9, scroller: this.scroller }) {
          ForEach(this.view.rows, (row: ChatRow) => {
            ListItem() { this.message(row) }
          }, (row: ChatRow) => row.event_id + '|' + row.progress + '|' + row.sending)
          ForEach(this.view.approvals, (approval: ChatApproval) => {
            ListItem() { this.approvalCard(approval) }
          }, (approval: ChatApproval): string => approval.interaction_id)
        }.layoutWeight(1).width('100%').padding({ left: 14, right: 14, top: 6, bottom: 12 })
          .scrollBar(BarState.Off)
          .onScrollIndex((_first: number, last: number): void => {
            this.following = last >= this.view.rows.length + this.view.approvals.length - 2;
          })
      }
      this.composer()
    }.height('100%').width('100%').backgroundColor(CANVAS)
  }
}
