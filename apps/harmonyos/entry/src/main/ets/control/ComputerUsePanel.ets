import { ComputerUseOwnerView, ComputerUseSurface } from './ComputerUseSurface';

@Component
export struct ComputerUsePanel {
  @StorageLink('amitaComputerUseOwnerView') view: ComputerUseOwnerView = new ComputerUseOwnerView();

  @Builder
  private actions(nonce: string, conflict: boolean, deciding: boolean) {
    Row({ space: 8 }) {
      if (conflict) {
        Button('继续原任务').fontSize(13).height(36).backgroundColor('#14644C')
          .enabled(!deciding).onClick((): void => { ComputerUseSurface.decide(nonce, 'continue'); })
      }
      Button('停止').fontSize(13).height(36).fontColor('#173D31').backgroundColor('#F4F7F3')
        .enabled(!deciding).onClick((): void => { ComputerUseSurface.decide(nonce, 'stop'); })
    }
  }

  build() {
    if (this.view.phase.length > 0) {
      Column({ space: 5 }) {
        Text(this.view.phase === 'conflict' ? '检测到本人操作，已暂停手机操控' :
          this.view.phase === 'operating' ? 'AMITA 正在读取共享屏幕' : '已确认继续，正在核对当前状态')
          .fontSize(15).fontWeight(FontWeight.Medium).fontColor('#173D31')
        if (this.view.canStop) {
          this.actions(this.view.nonce, this.view.phase === 'conflict', this.view.deciding)
        }
        if (this.view.decisionUnconfirmed) {
          Text('决定尚未确认').fontSize(12).fontColor('#545F58')
        }
      }
      .width('100%').alignItems(HorizontalAlign.Start)
      .padding({ left: 14, right: 14, top: 8, bottom: 8 }).backgroundColor('#E5EEE7')
    }
  }
}
