import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import test from 'node:test';

function load(name, imports) {
  const source = readFileSync(new URL(name + '.ets', import.meta.url), 'utf8')
    .replace(/^import[^\n]*\n/gm, '');
  const code = stripTypeScriptTypes(source, { mode: 'strip' }).replace(/^export /gm, '');
  return new Function(...Object.keys(imports), code + '\nreturn ' + name + ';')(...Object.values(imports));
}

function fixture({ liveviewEnabled = true, notificationEnabled = true } = {}) {
  let now = 1000;
  let sequence = 0;
  let active = null;
  let ownerView = null;
  const notices = [];
  const calls = [];
  const clock = { now: () => now };
  const liveViewManager = {
    ActionType: { ACTION_TYPE_OPEN_APP_PAGE: 1 },
    CapsuleType: { CAPSULE_TYPE_TEXT: 1 },
    ExtensionType: { EXTENSION_TYPE_COMMON_TEXT: 0 },
    LayoutType: { LAYOUT_TYPE_DEFAULT: -1 },
    LifeCycleMode: { AUTO_STOP_WHEN_APP_TERMINATE: 1 },
    isLiveViewEnabled: async () => liveviewEnabled,
    getActiveLiveView: async () => {
      if (active === null) { throw Object.assign(new Error('missing'), { code: 1003500009 }); }
      return structuredClone(active);
    },
    startLiveView: async view => {
      active = structuredClone(view); calls.push('start'); return { resultCode: 0 };
    },
    updateLiveView: async view => {
      assert.equal(view.id, active.id); assert.equal(view.sequence > active.sequence, true);
      active = structuredClone(view); calls.push('update'); return { resultCode: 0 };
    },
    stopLiveView: async view => {
      assert.equal(view.id, active.id); assert.equal(view.sequence > active.sequence, true);
      assert.equal(view.liveViewData.capsule.status, -1);
      active = null; calls.push('stop'); return { resultCode: 0 };
    }
  };
  const ApprovalSurface = load('ApprovalSurface', {
    Date: clock, deviceInfo: { sdkApiVersion: 26 }, liveViewManager,
    legacyNotification: { ContentType: { NOTIFICATION_CONTENT_BASIC_TEXT: 0 } },
    notificationManager: {
      isNotificationEnabled: async () => notificationEnabled,
      publish: async notice => { notices.push(notice); }, cancel: async () => { calls.push('cancel'); }
    }
  });
  const surface = load('ComputerUseSurface', { ApprovalSurface, Date: clock,
    CONTROL_BUNDLE: 'org.personalassistant.mesh', util: { generateRandomUUID: () => 'nonce-' + ++sequence },
    buildControlEntryWantAgent: async want => want, setTimeout: () => 1, clearTimeout: () => {},
    AppStorage: { setOrCreate: (_key, view) => { ownerView = view; } }
  });
  return { surface, notices, calls, advance: value => { now += value; },
    active: () => active, ownerView: () => ownerView,
    scope: { operationId: 'original-operation', taskId: 'original-task', expiresAt: 60000 } };
}

test('read-only activity stays silent and only an observed conflict uses notification fallback', async () => {
  const f = fixture({ liveviewEnabled: false });
  const operating = await f.surface.operating(f.scope, 'PROGRESS');
  assert.equal(operating.surface, 'in_app_status');
  assert.equal(operating.pending, false);
  assert.equal(operating.liveview_error, 'liveview_disabled');
  assert.equal(f.notices.length, 0);
  let decisions = 0;
  const conflict = await f.surface.conflict(f.scope, 'conflict-1', 10000, async () => { decisions++; return true; });
  assert.equal(conflict.surface, 'notification');
  assert.equal(conflict.pending, true);
  assert.equal(f.notices.length, 1);
  assert.equal(f.notices[0].autoDeletedTime, 10000);
  f.advance(9000);
  await f.surface.onNewWant(f.notices[0].actionButtons[0].wantAgent);
  assert.equal(decisions, 0);
  await f.surface.close(f.scope);
  assert.equal(f.ownerView().phase, '');
});

test('Live View actions are bound to the current conflict and only the provider can report resumed activity', async () => {
  const f = fixture();
  const decisions = [];
  await f.surface.operating(f.scope, 'PROGRESS');
  assert.equal(f.active().isMute, true);
  assert.equal(f.active().liveViewData.capsule.status, 1);
  await f.surface.conflict(f.scope, 'conflict-1', 12500, async decision => { decisions.push(decision); return true; });
  const conflictView = f.active().liveViewData.primary;
  const actions = conflictView.layoutData.serviceButtons;
  await f.surface.onNewWant(conflictView.clickAction);
  assert.deepEqual(decisions, []);
  await f.surface.onNewWant(actions[0].clickAction);
  await f.surface.onNewWant(actions[1].clickAction);
  assert.deepEqual(decisions, ['continue']);
  assert.equal(f.ownerView().phase, 'resuming');
  await f.surface.operating(f.scope, 'PROGRESS');
  await f.surface.conflict(f.scope, 'conflict-1', 12500, async decision => { decisions.push(decision); return true; });
  assert.equal(f.ownerView().phase, 'operating');
  await f.surface.close({ ...f.scope, operationId: 'different-operation' });
  assert.notEqual(f.active(), null);
  await f.surface.close(f.scope);
  assert.equal(f.active(), null);
  assert.equal(f.notices.length, 0);
});
