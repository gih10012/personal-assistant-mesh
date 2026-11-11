import assert from 'node:assert/strict';
import test from 'node:test';
const { LocalRepairController, noLocalRepairs, parseRepairPlan } = await import('../entry/src/main/ets/selfmaintenance/LocalRepair.ts');
const { restoreSupervisor } = await import('../entry/src/main/ets/selfmaintenance/SelfMaintenance.ts');

function repairFixture() {
  const plan = {format:'amita-phone-maintenance-repair/1',incident_id:'original-incident',task_id:'original-task',
    task_epoch:2,operation_id:'original-operation',action:'rollback_signed_data',target_version:7};
  const row = {incident_id:plan.incident_id,snapshot:{diagnostic:'signed_data_sync',state:'verification_failed',data_version:8,pending_receipts:0},
    state:'finished',task_id:plan.task_id,task_epoch:2,operation_id:plan.operation_id,task_status:'completed',result:JSON.stringify(plan),recovered:false};
  const state = {format:'mesh-phone-supervisor/1',enabled:true,records:[row]};
  let durable = structuredClone(state), calls = 0, active = 8, failSave = false;
  const host = {
    remaining: () => 20000,
    async save(value) { if (failSave) throw new Error('disk unavailable'); durable = structuredClone(value); },
    async capture() { return {active_version:8,target_version:7,target_manifest_sha256:'a'.repeat(64)}; },
    async execute() {
      assert.equal(durable.records[0].repair.state, 'attempted');
      calls++; active = 7; throw new Error('lost local completion ACK');
    },
    async observe(plan, checkpoint) { return {outcome:active === checkpoint.target_version ? 'confirmed':'unconfirmed',
      active_version:active,observed_receipts:0,target_manifest_sha256:active === 7 ? 'a'.repeat(64):''}; }
  };
  const policy = {sync_signed_data:true,observe_receipts:true,rollback_versions:[7]};
  return {state,row,plan,host,policy,get calls(){return calls;},get durable(){return durable;}, failSave(){failSave=true;}};
}

function roleRecommendation(f, action = 'recommend_signed_data_sync') {
  return {format:'amita-phone-maintenance-repair/1',task_id:f.row.task_id,incident_id:f.row.incident_id,
    operation_id:f.row.operation_id,decision:'recommendation',actions:[action],
    original_receipt_ids:['model-mentioned-receipt'],reason:'Diagnosed the original bounded device snapshot.'};
}

test('actual role recommendations normalize only two safe actions using the original Source epoch', async () => {
  const f = repairFixture();
  for (const [recommendation, action] of [['recommend_signed_data_sync','sync_signed_data'],
    ['reconcile_original_receipts','observe_receipts']]) {
    const reply = roleRecommendation(f, recommendation);
    assert.deepEqual(parseRepairPlan(JSON.stringify(reply), f.row), {
      format:f.plan.format,incident_id:f.row.incident_id,task_id:f.row.task_id,task_epoch:2,
      operation_id:f.row.operation_id,action,target_version:0
    });
  }
  f.row.result = JSON.stringify(roleRecommendation(f));
  await assert.rejects(new LocalRepairController(f.state,f.host,noLocalRepairs()).advance(f.row), /owner_scope/);
  assert.equal(f.row.repair, undefined); assert.equal(f.calls, 0);
  f.host.capture = async plan => {
    assert.equal(plan.action, 'sync_signed_data');
    assert.equal('original_receipt_ids' in plan, false);
    return {active_version:8,target_version:0,target_manifest_sha256:''};
  };
  let executions = 0, observations = 0;
  f.host.execute = async () => { executions++; throw new Error('lost original completion ACK'); };
  f.host.observe = async plan => {
    observations++; assert.equal('original_receipt_ids' in plan, false);
    return {outcome:'unconfirmed',active_version:8,observed_receipts:0,target_manifest_sha256:''};
  };
  const controller = new LocalRepairController(f.state,f.host,f.policy);
  await assert.rejects(controller.advance(f.row), /original_attempt_unconfirmed/);
  assert.equal((await controller.advance(f.row)).state, 'unknown');
  assert.equal(executions, 1); assert.equal(observations, 1);
});

test('role adapter rejects foreign identities, unknown or multiple actions and noncompleted receipts before execution', async () => {
  const f = repairFixture(), reply = roleRecommendation(f);
  for (const extra of [{task_id:'foreign'}, {incident_id:'foreign'}, {operation_id:'foreign'},
    {task_epoch:2}, {shell:'untrusted'}, {decision:'no_action'}, {decision:'escalate_leader'},
    {actions:['diagnose']}, {actions:['unknown']}, {actions:['rollback_signed_data']},
    {actions:['recommend_signed_data_sync','reconcile_original_receipts']}, {actions:[]},
    {reason:'x'.repeat(1001)}, {original_receipt_ids:Array(17).fill('receipt')},
    {original_receipt_ids:['duplicate','duplicate']}, {original_receipt_ids:['x'.repeat(161)]}]) {
    f.row.result = JSON.stringify({...reply,...extra});
    await assert.rejects(new LocalRepairController(f.state,f.host,f.policy).advance(f.row));
    assert.equal(f.row.repair, undefined);
  }
  for (const changed of [{state:'unknown'}, {task_status:'needs_review'}, {task_epoch:0},
    {task_epoch:undefined}, {task_epoch:1.5}]) {
    assert.throws(() => parseRepairPlan(JSON.stringify(reply), {...f.row,...changed}));
  }
  assert.equal(f.calls, 0);
});

test('repair owner scope and strict original result binding reject Shell, URL, epoch/task swaps before an effect', async () => {
  const f = repairFixture();
  await assert.rejects(new LocalRepairController(f.state,f.host,noLocalRepairs()).advance(f.row), /owner_scope/);
  assert.equal(f.calls, 0); assert.equal(f.row.repair, undefined);
  for (const extra of [{shell:'rm anything'}, {url:'https://untrusted/'}, {action:'install_hap'},
    {task_epoch:3}, {task_id:'replacement'}, {incident_id:'other'}, {operation_id:'other'}]) {
    assert.throws(() => parseRepairPlan(JSON.stringify({...f.plan,...extra}), f.row));
  }
  f.failSave();
  await assert.rejects(new LocalRepairController(f.state,f.host,f.policy).advance(f.row));
  assert.equal(f.calls, 0); // Even a valid model result cannot act without durable intent.
});

test('an exhausted OS window retains the same plan without a repair intent or execution', async () => {
  const f = repairFixture(); f.host.remaining = () => 1000;
  const before = structuredClone(f.state);
  assert.equal(await new LocalRepairController(f.state,f.host,f.policy).advance(f.row), null);
  assert.deepEqual(f.state, before); assert.equal(f.calls, 0);
  const afterRead = repairFixture(); let remaining = 20000;
  afterRead.host.remaining = () => remaining;
  const capture = afterRead.host.capture;
  afterRead.host.capture = async plan => { const value = await capture(plan); remaining = 1000; return value; };
  assert.equal(await new LocalRepairController(afterRead.state,afterRead.host,afterRead.policy).advance(afterRead.row), null);
  assert.equal(afterRead.row.repair, undefined); assert.equal(afterRead.calls, 0);
});

test('lost rollback completion and cold intent use original read-only observation, never a second rollback', async () => {
  const f = repairFixture();
  const controller = new LocalRepairController(f.state,f.host,f.policy);
  await assert.rejects(controller.advance(f.row));
  assert.equal(f.calls, 1); assert.equal(f.row.repair.state, 'unknown');
  const oldResult = f.row.result;
  restoreSupervisor(f.state);
  assert.equal((await controller.advance(f.row)).state, 'completed');
  await controller.advance(f.row);
  assert.equal(f.calls, 1); assert.equal(f.row.result, oldResult);
  const cold = repairFixture();
  cold.row.repair = {format:'amita-local-repair-record/1',plan_json:JSON.stringify(cold.plan),state:'intent',
    checkpoint:{active_version:8,target_version:7,target_manifest_sha256:'a'.repeat(64)},evidence:null,error_code:0};
  restoreSupervisor(cold.state);
  assert.equal((await new LocalRepairController(cold.state,cold.host,cold.policy).advance(cold.row)).state,'unknown');
  assert.equal(cold.calls,0);
});
