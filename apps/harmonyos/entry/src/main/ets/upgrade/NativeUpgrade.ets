import { common, bundleManager } from '@kit.AbilityKit';
import { updateManager } from '@kit.AppGalleryKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { NodeCredential, NodeCredentialStore } from '../security/NodeCredentialStore';
import { InstalledConfig, readInstalledConfig } from '../update/InstalledConfig';
import { DistributionLink, validateDistributionLink } from './DistributionConfig';
export interface NativeUpgradeStatus {
  state: string;
  installedVersion: number;
  updateAvailable: boolean;
  dialogShown: boolean;
  installationVerified: boolean;
  errorCode: number;
}

// Called from explicit owner UI gestures. AppGallery itself checks release
// eligibility; a shown dialog/opened link never proves native installation.
export class NativeUpgrade {
  private context: common.UIAbilityContext;
  private foreground: () => boolean;
  private current: NativeUpgradeStatus = { state: 'not_checked', installedVersion: 0,
    updateAvailable: false, dialogShown: false, installationVerified: false, errorCode: 0 };
  constructor(context: common.UIAbilityContext, foreground: () => boolean) {
    this.context = context; this.foreground = foreground;
  }
  private requireForeground(): void {
    if (!this.foreground()) { throw new Error('native_upgrade_foreground_user_action_required'); }
  }
  private failed(error: Error): void {
    const code: number = (error as BusinessError).code;
    this.current.state = 'unavailable'; this.current.errorCode = Number.isSafeInteger(code) ? code : -1;
    this.current.dialogShown = false; this.current.installationVerified = false;
  }
  status(): NativeUpgradeStatus {
    return { state: this.current.state, installedVersion: this.current.installedVersion,
      updateAvailable: this.current.updateAvailable, dialogShown: this.current.dialogShown,
      installationVerified: false, errorCode: this.current.errorCode };
  }
  async checkAppGallery(): Promise<NativeUpgradeStatus> {
    this.requireForeground();
    try {
      const installed: bundleManager.BundleInfo = await bundleManager.getBundleInfoForSelf(bundleManager.BundleFlag.GET_BUNDLE_INFO_DEFAULT);
      this.current.installedVersion = installed.versionCode;
      const result: updateManager.CheckUpdateResult = await updateManager.checkAppUpdate(this.context);
      this.requireForeground();
      this.current.updateAvailable = result.updateAvailable === updateManager.UpdateAvailableCode.LATER_VERSION_EXIST;
      this.current.state = this.current.updateAvailable ? 'available_user_confirmation_required' : 'store_reported_no_update';
      this.current.errorCode = 0; this.current.dialogShown = false;
      return this.status();
    } catch (error) { this.failed(error as Error); throw new Error('native_store_check_unavailable'); }
  }
  async showAppGalleryDialog(): Promise<NativeUpgradeStatus> {
    this.requireForeground();
    if (!this.current.updateAvailable) { throw new Error('native_store_update_not_observed'); }
    try {
      const result: updateManager.ShowUpdateResultCode = await updateManager.showUpdateDialog(this.context);
      this.current.dialogShown = result === updateManager.ShowUpdateResultCode.SHOW_DIALOG_SUCCESS;
      this.current.state = this.current.dialogShown ? 'dialog_shown_install_unconfirmed' : 'dialog_not_shown';
      if (!this.current.dialogShown) { throw new Error('native_store_dialog_failed'); }
      return this.status();
    } catch (error) { this.failed(error as Error); throw new Error('native_store_dialog_unavailable'); }
  }
  private async configured(): Promise<string> {
    const credential: NodeCredential | null = await new NodeCredentialStore().load();
    if (credential === null) { throw new Error('native_distribution_original_node_required'); }
    const config: InstalledConfig | null = await readInstalledConfig(this.context, credential.nodeId, 'config/native-distribution.json');
    if (config === null) { throw new Error('native_signed_distribution_config_missing'); }
    return config.raw;
  }
  async openConfiguredDistributionLink(): Promise<NativeUpgradeStatus> {
    this.requireForeground();
    return this.openDistributionLink(await this.configured());
  }
  // Compatibility for trusted callers: even provided bytes must exactly match
  // the current signed active file, never an unsigned model/network string.
  async openOwnerDistributionLink(raw: string): Promise<NativeUpgradeStatus> {
    this.requireForeground();
    if (raw !== await this.configured()) { throw new Error('native_signed_distribution_config_required'); }
    return this.openDistributionLink(raw);
  }
  private async openDistributionLink(raw: string): Promise<NativeUpgradeStatus> {
    this.requireForeground();
    if (raw.length > 4096) { throw new Error('native_distribution_config_budget'); }
    const link: DistributionLink = JSON.parse(raw) as DistributionLink;
    const installed: bundleManager.BundleInfo = await bundleManager.getBundleInfoForSelf(bundleManager.BundleFlag.GET_BUNDLE_INFO_DEFAULT);
    validateDistributionLink(link, installed.name, Math.floor(Date.now() / 1000));
    try {
      this.requireForeground();
      await this.context.openLink(link.url, { appLinkingOnly: false });
      this.current.installedVersion = installed.versionCode;
      this.current.state = 'distribution_link_opened_install_unconfirmed';
      this.current.updateAvailable = false; this.current.dialogShown = false;
      this.current.errorCode = 0;
      return this.status();
    } catch (error) { this.failed(error as Error); throw new Error('native_distribution_link_unavailable'); }
  }
}
