import { BusinessError } from '@kit.BasicServicesKit';

export function maintenanceErrorCode(error: Error): number {
  const code: number = (error as BusinessError).code;
  return Number.isSafeInteger(code) ? code : -1;
}

// A dependency can preserve an actual SDK code without persisting/logging its
// message, URL, file path, request headers or credential contents.
export class MaintenanceApiError extends Error {
  code: number;
  constructor(error: Error) {
    super('maintenance_api_unconfirmed');
    this.code = maintenanceErrorCode(error);
  }
}
