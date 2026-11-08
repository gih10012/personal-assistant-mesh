import { abilityAccessCtrl, common } from '@kit.AbilityKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { screenshot } from '@kit.ArkUI';
import { image } from '@kit.ImageKit';
import { ControlFence } from './ControlSession';

export interface GlobalCaptureStatus {
  capability: string; permission_granted: boolean; owner_authorised: boolean;
  platform_capture_verified: boolean; continuous_stream: boolean; last_error: string; last_error_code: number;
}
export interface GlobalScreenImage {
  format: string; bytes: Uint8Array; width: number; height: number; display_id: number;
  crop_left: number; crop_top: number; scale_x: number; scale_y: number;
  user_selected: boolean; global_capture: boolean; continuous_stream: boolean;
}

// Separate from SelectedScreenCapture: this really calls screenshot.capture,
// after normal CUSTOM_SCREEN_CAPTURE user permission and local owner consent.
// It is one bounded call, not a background residency or continuous-stream claim.
export class GlobalScreenCapture {
  private access: abilityAccessCtrl.AtManager = abilityAccessCtrl.createAtManager();
  private ownerAuthorised: boolean = false;
  private busy: boolean = false;
  private verified: boolean = false;
  private lastCaptureAt: number = 0;
  private lastError: string = '';
  private lastErrorCode: number = 0;

  private permissionGranted(): boolean {
    try {
      return this.access.getSelfPermissionStatus('ohos.permission.CUSTOM_SCREEN_CAPTURE') ===
        abilityAccessCtrl.PermissionStatus.GRANTED;
    } catch (error) {
      this.lastError = 'global_capture_permission_query_unavailable';
      const code: number = (error as BusinessError).code;
      this.lastErrorCode = [201, 202, 401, 801].includes(code) ? code : 0;
      throw new Error('phone_global_capture_permission_query_unavailable');
    }
  }
  status(): GlobalCaptureStatus {
    let granted: boolean = false;
    try { granted = this.permissionGranted(); } catch (error) {}
    return { capability: 'global_screen_capture', permission_granted: granted,
      owner_authorised: this.ownerAuthorised, platform_capture_verified: this.verified,
      continuous_stream: false, last_error: this.lastError, last_error_code: this.lastErrorCode };
  }
  setOwnerCaptureAllowed(value: boolean): void { this.ownerAuthorised = value; }
  // Root calls only from the real owner's UI gesture. The manifest must already
  // declare CUSTOM_SCREEN_CAPTURE with a real reason/usedScene; no system ACL.
  async requestPermissionFromOwnerGesture(context: common.UIAbilityContext): Promise<boolean> {
    try {
      if (!this.permissionGranted()) {
        await this.access.requestPermissionsFromUser(context, ['ohos.permission.CUSTOM_SCREEN_CAPTURE']);
      }
      return this.permissionGranted();
    } catch (error) {
      this.lastError = 'global_capture_permission_unavailable';
      const code: number = (error as BusinessError).code;
      this.lastErrorCode = [201, 202, 401, 801, 12100009, 12100014].includes(code) ? code : 0;
      throw new Error('phone_global_capture_permission_unavailable');
    }
  }
  private allowed(deadline: number, fence: ControlFence): void {
    if (!this.ownerAuthorised || !fence.valid || !Number.isSafeInteger(deadline) || deadline <= Date.now() ||
      deadline > Date.now() + 30000 || !this.permissionGranted()) {
      throw new Error('phone_global_capture_permission_or_local_approval_required');
    }
  }
  async captureApproved(deadline: number, fence: ControlFence): Promise<GlobalScreenImage> {
    this.allowed(deadline, fence);
    if (this.busy || Date.now() - this.lastCaptureAt < 1000) { throw new Error('phone_global_capture_busy_or_rate_limited'); }
    this.busy = true; this.lastCaptureAt = Date.now();
    try {
      const pixels: image.PixelMap = await screenshot.capture({ displayId: 0 });
      try {
        this.allowed(deadline, fence);
        const original: image.ImageInfo = await pixels.getImageInfo();
        const factor: number = Math.min(1, 1280 / original.size.width, 1280 / original.size.height,
          Math.sqrt(1048576 / (original.size.width * original.size.height)));
        if (!Number.isFinite(factor) || factor <= 0) { throw new Error('phone_global_capture_dimensions_invalid'); }
        if (factor < 1) { await pixels.scale(factor, factor); }
        const dimensions: image.ImageInfo = await pixels.getImageInfo();
        const packer: image.ImagePacker = image.createImagePacker();
        try {
          const bytes: ArrayBuffer = await packer.packing(pixels, { format: 'image/jpeg', quality: 60 });
          if (bytes.byteLength > 262144 || dimensions.size.width * dimensions.size.height > 1048576) {
            throw new Error('phone_global_capture_budget_exceeded');
          }
          this.allowed(deadline, fence);
          this.verified = true; this.lastError = ''; this.lastErrorCode = 0;
          return { format: 'image/jpeg', bytes: new Uint8Array(bytes), width: dimensions.size.width,
            height: dimensions.size.height, display_id: 0, crop_left: 0, crop_top: 0,
            scale_x: dimensions.size.width / original.size.width, scale_y: dimensions.size.height / original.size.height,
            user_selected: false, global_capture: true, continuous_stream: false };
        } finally { await packer.release(); }
      } finally { await pixels.release(); }
    } catch (error) {
      const code: number = (error as BusinessError).code;
      this.lastErrorCode = [201, 202, 401, 801, 1400003].includes(code) ? code : 0;
      this.lastError = code === 201 ? 'global_capture_permission_denied' :
        code === 801 ? 'global_capture_platform_unavailable' : 'global_capture_unconfirmed';
      throw new Error('phone_global_capture_unconfirmed');
    } finally { this.busy = false; }
  }
}
