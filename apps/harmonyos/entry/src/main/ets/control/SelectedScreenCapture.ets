import { screenshot } from '@kit.ArkUI';
import { image } from '@kit.ImageKit';

export interface SelectedScreenImage {
  format: string;
  bytes: Uint8Array;
  width: number;
  height: number;
  crop_left: number;
  crop_top: number;
  scale_x: number;
  scale_y: number;
  user_selected: boolean;
  continuous_capture: boolean;
}

// Explicit owner interaction only. This is not a continuous/global observation
// capability and cannot be called as a silent remote screenshot fallback.
export class SelectedScreenCapture {
  async pickFromOwnerGesture(): Promise<SelectedScreenImage> {
    const selected: screenshot.PickInfo = await screenshot.pick();
    const packer: image.ImagePacker = image.createImagePacker();
    try {
      const original: image.ImageInfo = await selected.pixelMap.getImageInfo();
      const factor: number = Math.min(1, 1280 / original.size.width, 1280 / original.size.height,
        Math.sqrt(1048576 / (original.size.width * original.size.height)));
      if (!Number.isFinite(factor) || factor <= 0) { throw new Error('phone_selected_capture_dimensions_invalid'); }
      if (factor < 1) { await selected.pixelMap.scale(factor, factor); }
      const dimensions: image.ImageInfo = await selected.pixelMap.getImageInfo();
      const data: ArrayBuffer = await packer.packing(selected.pixelMap, { format: 'image/jpeg', quality: 60 });
      if (data.byteLength > 262144 || dimensions.size.width * dimensions.size.height > 1048576) {
        throw new Error('phone_selected_capture_budget_exceeded');
      }
      return { format: 'image/jpeg', bytes: new Uint8Array(data), width: dimensions.size.width,
        height: dimensions.size.height, crop_left: selected.pickRect.left, crop_top: selected.pickRect.top,
        scale_x: dimensions.size.width / original.size.width, scale_y: dimensions.size.height / original.size.height,
        user_selected: true, continuous_capture: false };
    } catch (error) { throw new Error('phone_selected_capture_unavailable'); }
    finally { await packer.release(); await selected.pixelMap.release(); }
  }
}
