import { InputMethodExtensionAbility, inputMethodEngine } from '@kit.IMEKit';
import { display } from '@kit.ArkUI';
import { EditorReview } from './EditorReview';

export default class AmitaInputMethod extends InputMethodExtensionAbility {
  private engine: inputMethodEngine.InputMethodAbility | undefined;
  private delegate: inputMethodEngine.KeyboardDelegate | undefined;
  private review: EditorReview | undefined;
  private panel: inputMethodEngine.Panel | undefined;
  private destroyed: boolean = false;
  private visible: boolean = false;

  private readonly inputStart = (_: inputMethodEngine.KeyboardController, client: inputMethodEngine.InputClient): void => {
    this.review?.bind(client);
  };

  private readonly inputStop = (): void => {
    this.review?.bind(undefined);
    this.keyboardHide();
  };

  private readonly keyboardShow = (): void => {
    this.visible = true;
    this.review?.setVisible(true);
    this.panel?.show().catch(() => this.review?.setVisible(false));
  };

  private readonly keyboardHide = (): void => {
    this.visible = false;
    this.review?.setVisible(false);
    this.panel?.hide().catch(() => {});
  };

  private readonly targetChanged = (): void => {
    this.review?.invalidate();
  };

  onCreate(): void {
    const engine = inputMethodEngine.getInputMethodAbility();
    const delegate = inputMethodEngine.getKeyboardDelegate();
    this.engine = engine;
    this.delegate = delegate;
    const review = new EditorReview(engine);
    this.review = review;
    engine.on('inputStart', this.inputStart);
    engine.on('inputStop', this.inputStop);
    engine.on('keyboardShow', this.keyboardShow);
    engine.on('keyboardHide', this.keyboardHide);
    engine.on('setCallingWindow', this.targetChanged);
    engine.on('securityModeChange', this.targetChanged);
    engine.on('discardTypingText', this.targetChanged);
    delegate.on('editorAttributeChanged', this.targetChanged);
    this.createPanel(engine, review).catch(() => this.review?.setVisible(false));
  }

  onDestroy(): void {
    this.destroyed = true;
    this.review?.bind(undefined);
    const engine = this.engine;
    if (engine !== undefined) {
      engine.off('inputStart', this.inputStart);
      engine.off('inputStop', this.inputStop);
      engine.off('keyboardShow', this.keyboardShow);
      engine.off('keyboardHide', this.keyboardHide);
      engine.off('setCallingWindow', this.targetChanged);
      engine.off('securityModeChange', this.targetChanged);
      engine.off('discardTypingText', this.targetChanged);
      if (this.panel !== undefined) {
        engine.destroyPanel(this.panel).catch(() => {});
        this.panel = undefined;
      }
    }
    this.delegate?.off('editorAttributeChanged', this.targetChanged);
  }

  private async createPanel(engine: inputMethodEngine.InputMethodAbility, review: EditorReview): Promise<void> {
    const panelInfo: inputMethodEngine.PanelInfo = {
      type: inputMethodEngine.PanelType.SOFT_KEYBOARD,
      flag: inputMethodEngine.PanelFlag.FLG_FIXED
    };
    const panel = await engine.createPanel(this.context, panelInfo);
    try {
      if (this.destroyed) {
        return;
      }
      await panel.setUiContent('pages/EditorReview', review.storage);
      if (this.destroyed) {
        return;
      }
      const screen = display.getDefaultDisplaySync();
      const shortEdge = Math.min(screen.width, screen.height);
      const longEdge = Math.max(screen.width, screen.height);
      const keyboardHeight = Math.floor(shortEdge * 0.60);
      await panel.updatePanelRect(inputMethodEngine.PanelFlag.FLG_FIXED, {
        portraitRect: { left: 0, top: longEdge - keyboardHeight, width: shortEdge, height: keyboardHeight },
        landscapeRect: { left: 0, top: shortEdge - keyboardHeight, width: longEdge, height: keyboardHeight }
      });
      if (this.destroyed) {
        return;
      }
      this.panel = panel;
      if (this.visible) {
        this.keyboardShow();
      }
    } finally {
      if (this.panel !== panel) {
        await engine.destroyPanel(panel).catch(() => {});
      }
    }
  }
}
