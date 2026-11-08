# Background supervision: scope and integration

This leaf supplies actual bounded OS callback supervision of the existing signed
data updater. It does **not** claim that a model is installed, that a callback
occurred on this phone, or that arbitrary business work can remain alive forever.
The ordinary phone credential has no authorized cognition-delegation endpoint.
`MaintenanceSupervisor.status()` explicitly reports that missing route.

Root integrates one existing module entry: keep extension name `AmitaUpdateWork`,
type `workScheduler`, exported false and work ID 3101; change only its `srcEntry`
to `./ets/maintenance/AmitaMaintenanceWork.ets`. Keep existing `Schedule.ts/ets`
conditions and four-hour preference. Do not register a second job, reset IDs,
change Asset credentials or expand the original phone/task authorization.
This leaf deliberately does not edit module.json5 or the owner's UI/core files.

The new callback invokes the real UpdateCoordinator: bounded short HTTP, 90s,
1MiB overall, 256KiB small artifacts, pinned signature/node/key, exact HTTPS
routes, full-byte hash checks and the existing update DB's compare-and-set lease.
On OS stop it destroys the original request; it never reissues business work.
It runs without a UIAbility, selected-file permission, request.agent or model.

`amita_maintenance_v1.db` stores callback start, outcome, OS stop flag, timestamp,
active data version and pending-foreground state. An abruptly terminated
callback retains `running_or_interrupted_unknown` with ended=0. It does not
become success just because the extension restarted. Completed observations
retain the latest 128; unfinished rows are never discarded by that bound.
OS stops are recorded as stopped; other exceptions are unconfirmed, never
mislabelled as a known failure caused by suspension. Errors are recorded and
emitted as fixed diagnostics, not swallowed into a successful result.
No token, URL or user-file contents are logged. The journal
observes maintenance and never replaces the business/operation ledger.

The old UpdateCoordinator currently wraps SDK exceptions in a generic fixed
Error and discards their code. The new journal accurately records -1 (unknown)
for that case. To preserve actual API codes during Root's integration, import
`MaintenanceApiError` from `../maintenance/MaintenanceError` in the updater and
replace its existing fixed-error throw in the catch with
`throw new MaintenanceApiError(error as Error)`. Keep its existing DB error
record/finally/old-version behavior. This helper retains only a numeric code,
never raw SDK messages. This leaf does not modify that shared dependency.
NativeUpgrade already catches its direct SDK calls and retains their codes.

For owner UI diagnostics, `new MaintenanceSupervisor(context).status()` reads
these callback records. `scheduled=true` in the original updater means only OS
registration. It is not proof of callback execution, permanent availability,
network connectivity, inference or a background business dispatch.

## API26 evidence and real eligibility

Reviewed installed commercial SDK: CLT26.0.0.851 / SDK26.0.0.105, under
`sdk/default/openharmony/ets/api` and `sdk/default/hms/ets/api`. The following
conclusions use its real declarations and Huawei primary documentation:

| Mechanism | Actual available behavior / remaining gate |
| --- | --- |
| WorkScheduler | System may defer or omit execution; single callback at most two minutes. Current 90s updater fits; foreground callbacks do not prove this callback. [Reference](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/js-apis-workschedulerextensionability/). |
| Transient task | Short completion window with quota; not an autonomous daemon. Use for genuine short in-flight completion only. [Background guide](https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/continuous-task/). |
| Ordinary continuous task | Must match a real declared user-visible business, end when finished, and honor OS/user cancellation. Empty data transfer or fake audio is forbidden. [Access rules](https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/bgtask-design-formula/). |
| Phone computation / special scenario | MODE_TASK_KEEPING on phone needs system ACL. API24+ special scenario needs KEEP_BACKGROUND_RUNNING_SPECIAL_SCENARIO plus user authorization and a qualifying subtype. General agent polling is not one of the documented media/casting/indoor-sport scenes. API26's dialog method does not bypass the ACL. [Actual API](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/js-apis-resourceschedule-backgroundtaskmanager/). |
| AppServiceExtension | Only PC/2in1; enterprise common-app SUPPORT_APP_SERVICE_EXTENSION ACL. It is not a regular phone daemon. [Reference](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/js-apis-app-ability-appserviceextensionability/). |
| Push background data | Can deliver/cache data, including system-written pushmessage.db; ordinary background message receipt does not wake arbitrary app logic. [Official scenario contract](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/push-scenariozed-api-intro/), [receiver API](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/push-pushservice/). |
| PushExtension | System apps only, not open for application; independent process normally below ten seconds. Do not use voice/VoIP notifications as maintenance keepalive. [Reference](https://developer.huawei.com/consumer/cn/doc/doccenter-references/api/push-extension-ability/). |
| AgentExtension API24 | A system host connects to the component's A2A/auth/data lifecycle. This is not a promise of autonomous continuous lifetime. AgentCard/module registration, real host integration and authorization remain necessary. [Guide](https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/agent-extension-ability/). |
| AgentFrameworkKit | SDK requires a real created agentId, support check, user privacy consent and Huawei sign-in. Its presence is not a qualified free model route or authorization to move personal credentials. SDK `@hms.ai.AgentFramework.d.ets` links the [official creation flow](https://developer.huawei.com/consumer/cn/doc/service/quick-start-0000002469548009). |

No actual account/AGC ACL, agent registration, free inference entitlement or
device callback has been verified by this source leaf. Root owns those actions.
Request only an actual qualifying entitlement; do not advertise granted status
from an SDK declaration. If continuous phone execution is unavailable, route
continuous coordination to eligible Mesh peers and let this phone retain its
durable work and bounded OS maintenance role. That requires a real delegated
task/attempt/result contract and authorized cognition route; no stub is called
an autonomous agent here. Keep original unknowns and do not replay effects.

## Last USB window: concrete owner actions

1. Integrate this callback path and the separate native-upgrade UI service, build
   with the real SDK and original signer/Profile, increase native versionCode,
   then perform the same-signature installation. Do not uninstall to bypass a
   signature mismatch: node identity, Asset and durable state must survive.
2. Confirm actual foreground enrollment/business call separately. Then leave
   the UI, use the phone normally and observe the real WorkScheduler callback
   and its persisted outcome when scheduled. Reboot/first unlock and return
   later to read the original records. A registered job alone is not acceptance.
3. Before declaring USB permanently unnecessary, establish an authorized AGC
   designated-device or released-AppGallery channel, and actually install one
   increasing native version through that channel with user/system confirmation.
   Preserve the same app identifier/signer, node_id, Asset and old unknowns.
   See [native release contract](../upgrade/RELEASE.md).
4. Keep wireless HDC closed. Data config/skills/files continue through the
   existing signed data-manifest updater. An executable/HAP is not data and is
   still rejected by its path validator. New native permissions/APIs require a
   legitimate native release, not a downloaded script or a manifest toggle.
