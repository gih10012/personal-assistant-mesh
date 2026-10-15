// Presence/config check only: never installs SDK, signs, pairs or downloads.
import { existsSync, readFileSync } from 'node:fs';
import { delimiter, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
function locate(name) {
  const candidates = (process.env.PATH ?? '').split(delimiter).map(p => join(p, name));
  return candidates.some(p => existsSync(p));
}
const project = JSON.parse(readFileSync(join(root, 'build-profile.json5'), 'utf8'));
const module = JSON.parse(readFileSync(join(root, 'entry/src/main/module.json5'), 'utf8'));
const tools = { hvigor: locate('hvigorw') || locate('hvigor'), ohpm: locate('ohpm'), hdc: locate('hdc') };
const sdk = process.env.DEVECO_SDK_HOME || process.env.OHOS_SDK_HOME || '';
const sdkConfigured = sdk.length > 0 && existsSync(sdk);
const report = { kind: 'harmonyos-build-preflight/1', tools, sdk_configured: sdkConfigured,
  signing_configuration_empty: project.app.signingConfigs.length === 0,
  only_declared_permission: module.module.requestPermissions.map(p => p.name),
  compile_attempted: false, hap_compiled: false, device_paired_or_installed: false,
  ready_for_owner_selected_build: tools.hvigor && tools.ohpm && sdkConfigured };
process.stdout.write(JSON.stringify(report) + '\n');
if (!report.ready_for_owner_selected_build) process.exitCode = 2;
