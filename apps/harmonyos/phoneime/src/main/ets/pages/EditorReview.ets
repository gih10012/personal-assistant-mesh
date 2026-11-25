import { inputMethodEngine } from '@kit.IMEKit';
import { EditorReview } from '../EditorReview';

@Entry({ useSharedStorage: true })
@Component
struct EditorReviewPage {
  @LocalStorageProp('status') statusText: string = '';
  @LocalStorageProp('reviewId') reviewId: number = 0;
  @LocalStorageProp('reviewLabel') reviewLabel: string = '';
  private review: EditorReview | undefined;

  aboutToAppear(): void {
    this.review = this.getUIContext().getSharedLocalStorage()?.get<EditorReview>('review');
  }

  private fingerTap(event: ClickEvent): boolean {
    return event.source === SourceType.TouchScreen && event.sourceTool === SourceTool.Finger;
  }

  @Builder
  cursor(label: string, direction: inputMethodEngine.Direction) {
    Button(label)
      .layoutWeight(1)
      .focusable(false)
      .onClick((event: ClickEvent) => {
        if (this.fingerTap(event)) {
          this.review?.stageMove(direction);
        }
      })
  }

  @Builder
  confirmEdit(id: number) {
    Row({ space: 8 }) {
      Button('取消').layoutWeight(1).focusable(false).onClick(() => this.review?.cancel(id))
      Button('点按确认').layoutWeight(1).focusable(false).onClick((event: ClickEvent) => {
        if (this.fingerTap(event)) {
          this.review?.confirm(id);
        }
      })
    }.width('100%')
  }

  build() {
    Scroll() {
      Column({ space: 8 }) {
        Text('AMITA 编辑确认').fontSize(18).fontWeight(FontWeight.Medium)
        if (this.reviewId > 0) {
          Text(this.reviewLabel).fontSize(16)
          this.confirmEdit(this.reviewId)
        } else {
          Row({ space: 8 }) {
            Button('试填中文和表情').layoutWeight(1).focusable(false).onClick((event: ClickEvent) => {
              if (this.fingerTap(event)) {
                this.review?.stageInsert('你好，阿米塔 👋');
              }
            })
            Button('选择位置 0–2').layoutWeight(1).focusable(false).onClick((event: ClickEvent) => {
              if (this.fingerTap(event)) {
                this.review?.stageSelection(0, 2);
              }
            })
          }.width('100%')
          Row({ space: 8 }) {
            this.cursor('←', inputMethodEngine.Direction.CURSOR_LEFT)
            this.cursor('→', inputMethodEngine.Direction.CURSOR_RIGHT)
            this.cursor('↑', inputMethodEngine.Direction.CURSOR_UP)
            this.cursor('↓', inputMethodEngine.Direction.CURSOR_DOWN)
          }.width('100%')
        }
        Text(this.statusText).fontSize(14)
      }.alignItems(HorizontalAlign.Start).width('100%').padding(12)
    }.align(Alignment.Top).width('100%').height('100%')
  }
}
