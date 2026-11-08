# Native wireless version channel

`NativeUpgrade(context, isActuallyForeground)` is a UI-only service. Root can
wire explicit owner buttons to `checkAppGallery()`, `showAppGalleryDialog()`
and `status()` without changing the old tool/OAuth/phone grants. The predicate
must read the actual UIAbility lifecycle; a caller JSON boolean is not a grant.
The leaf does not edit Index.ets, NativeServices or module.json5.

The service calls the real API26 `@kit.AppGalleryKit.updateManager`. It returns
the installed native version from BundleManager; ordinary-app check results do
not contain an authoritative target versionCode. A successful check, shown
dialog or opened link is never recorded as installed. Failure preserves a
bounded SDK code and throws; no silent update, arbitrary HAP installer or
dynamic native patch engine is provided.

Huawei's [official application-update guide](https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/store-update/)
requires a **released AppGallery app**, a newer store version and matching
local/store signing information. It explicitly excludes invitation/public-test
versions from this check path. Both SDK calls require a foreground UIAbility;
error 1009400004 means the application is not in foreground. See the
[real updateManager reference](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/store-updatemanager/).
Therefore this API must not be presented as proof that today's debug HAP can
update itself wirelessly.

For the owner's test phone, supported owner-configured link types are
`agc-invitation-test` and `agc-designated-device`. The [official distribution comparison](https://developer.huawei.com/consumer/cn/doc/app/agc-help-invite-test-overview-0000002287701773/)
describes test distribution channels. This source has not verified actual
account eligibility, Profile lifetime or an installation on this phone. A
search-index FAQ excerpt is not proof of a 90-day entitlement or channel access.
Root must obtain the corresponding signing/distribution
eligibility in the existing Huawei account and test an actual same-identity
upgrade. Neither current debug Profile eligibility nor an AppGallery SDK import
proves access to that channel. Do not assume a debug-to-distribution signature
transition preserves app data; do not silently uninstall/recreate identity.

When Root has an actual approved distribution link, place this small config in
the **existing signed data manifest**, kind config at the fixed logical path
`config/native-distribution.json` (the existing PhoneFeatures path). Never accept unsigned network/model instructions:

```json
{
  "format": "amita-native-distribution-link/1",
  "bundle_name": "org.personalassistant.mesh",
  "channel": "agc-invitation-test",
  "url": "https://ACTUAL-OWNER-APPROVED-DISTRIBUTION-HOST/ACTUAL-LINK",
  "expires_at": 1
}
```

The sample URL/expiry are placeholders, deliberately unusable. On an explicit
user gesture call `openConfiguredDistributionLink()`. It re-verifies the active
original signed envelope, node trust, complete config artifact hash and current
pointer, then checks the installed bundle/expiry and opens the approved
HTTPS link through `UIAbilityContext.openLink`. That is a **system/browser
confirmation request**, not signature verification or installation success.
The compatibility `openOwnerDistributionLink(raw)` also requires provided bytes
to exactly match that currently active signed file; it cannot open raw unsigned
UI/model/network input. AppGallery's formal-release check remains a separate API
and does not acquire invitation-test support by adding this link channel.
The owner must review the original AGC domain/link before signing its config.
No HAP bytes pass through the config/skill/file updater. Platform installation
policy remains authoritative, including unavailable channels and user refusal.

Root's release receipt should retain bundle name, app identifier, actual signer
certificate SHA256, Profile type/expiry and allowed devices, increasing native
versionCode, compiled artifact size/SHA256 and source commit, and the real
distribution operation/link. Private keys, UDIDs, invitation links and tokens
stay out of the public repository. Protect the same signing key for all future
native versions. Match the official [debug/release signing distinctions](https://developer.huawei.com/consumer/cn/doc/App/agc-help-releasefaq-0000001110342644/).

After user-approved installation and relaunch, read actual BundleManager
versionCode again, confirm it increased to the expected receipt, confirm the
same app node_id and Asset credential still work, and issue one independent
original business request. Preserve before state, failed updates and unknown
business operations. A dialog/link acknowledgement alone is not that receipt.

The native channel and periodic data maintenance together make future updates
possible only once a legitimate distribution path has actually been accepted
on this phone. Until then, “today is the last USB” is a goal, not a proven
property. No campus wireless debugging or fake long-task scenario is used.
