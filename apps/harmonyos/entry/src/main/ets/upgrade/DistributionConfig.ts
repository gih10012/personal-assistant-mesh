export interface DistributionLink {
  format: string; bundle_name: string; channel: string; url: string; expires_at: number;
}
export function validateDistributionLink(link: DistributionLink, bundle: string, now: number): void {
  if (link === null || typeof link !== 'object' ||
    Object.keys(link).sort().join('|') !== 'bundle_name|channel|expires_at|format|url' ||
    link.format !== 'amita-native-distribution-link/1' || link.bundle_name !== bundle ||
    !['agc-designated-device', 'agc-invitation-test'].includes(link.channel) ||
    !Number.isSafeInteger(link.expires_at) || link.expires_at <= now || typeof link.url !== 'string' ||
    !/^https:\/\/[A-Za-z0-9.-]+\//.test(link.url) || /[\s\\@#]/.test(link.url) || link.url.length > 2048) {
    throw new Error('native_owner_distribution_link_invalid');
  }
}
