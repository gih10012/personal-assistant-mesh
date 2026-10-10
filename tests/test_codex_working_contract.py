"""Actual composed request fixtures, not production model/scheduler acceptance."""
import copy
import unittest
from unittest.mock import patch

from assistant_mesh.codex import Codex, CodexError, DEFAULT_INSTRUCTIONS, WORKING_CONTRACT, working_instructions


class CodexWorkingContractTests(unittest.TestCase):
    def runtime(self, **configuration):
        agent = Codex.__new__(Codex)
        agent.config = dict(workspace='/fixture/workspace', **configuration)
        agent.tools = [{'name': 'mesh'}]
        agent.on_activity = None
        agent.calls = []
        agent.deferred = []
        def rpc(method, parameters, **kwargs):
            agent.calls.append((method, copy.deepcopy(parameters)))
            if method in ('thread/start', 'thread/resume'):
                return {'thread': {'id': parameters.get('threadId', 'fixture-thread'),
                                   'model': 'fixture-account-model', 'path': '/fixture/selected-rollout.jsonl'}}
            if method == 'turn/start':
                return {'turn': {'id': 'fixture-turn'}}
            if method == 'model/list':
                return {'data': [{'id': 'fixture-catalog-model'}]}
            if method == 'thread/goal/get':
                return {'goal': None}
            if method == 'thread/goal/set':
                return {'goal': copy.deepcopy(parameters)}
            return {}
        agent.rpc = rpc
        return agent

    def test_start_payload_keeps_existing_default_and_appends_working_contract(self):
        agent = self.runtime()
        agent.start('actual requested work')
        method, request = agent.calls[0]
        self.assertEqual('thread/start', method)
        self.assertEqual(DEFAULT_INSTRUCTIONS + '\n\n' + WORKING_CONTRACT, request['developerInstructions'])
        self.assertEqual('/fixture/workspace', request['cwd'])
        self.assertEqual(agent.tools, request['dynamicTools'])
        self.assertNotIn('sandbox', request)
        self.assertNotIn('approvalPolicy', request)
        self.assertNotIn('model', request)
        self.assertEqual([{'type': 'text', 'text': 'actual requested work'}], agent.calls[-1][1]['input'])

    def test_custom_instructions_are_preserved_verbatim_in_start_and_resume(self):
        instructions = '  本人项目要求：保留格式\n第二行\n\n'
        for checkpoint in (None, {'thread_id': 'continuous-specialist'}):
            with self.subTest(checkpoint=checkpoint):
                agent = self.runtime(instructions=instructions)
                agent.start('continue requested work', checkpoint)
                request = agent.calls[0][1]
                self.assertEqual(instructions + '\n\n' + WORKING_CONTRACT, request['developerInstructions'])
                self.assertEqual(1, request['developerInstructions'].count(WORKING_CONTRACT))
                self.assertNotIn(DEFAULT_INSTRUCTIONS, request['developerInstructions'])

    def test_resume_keeps_native_identity_tools_plan_mode_without_restoring_checkpoint_goal(self):
        goal = {'objective': 'Keep full owner objective', 'status': 'active', 'tokenBudget': 1234,
                'tokensUsed': 900, 'timeUsedSeconds': 800}
        checkpoint = {'thread_id': 'continuous-owner-thread', 'native_rollout_path': '/private/selected.jsonl',
                      'goal': goal, 'turn_id': 'previous-turn', 'plan': [{'step': 'prior task', 'status': 'in_progress'}]}
        before = copy.deepcopy(checkpoint)
        agent = self.runtime(instructions='owner custom', mode='plan')
        result = agent.start('continue without replacing history', checkpoint)
        methods = [method for method, request in agent.calls]
        self.assertEqual(['thread/resume', 'thread/goal/get', 'turn/start'], methods)
        resumed = agent.calls[0][1]
        self.assertEqual('continuous-owner-thread', resumed['threadId'])
        self.assertEqual('/private/selected.jsonl', resumed['path'])
        self.assertIs(True, resumed['excludeTurns'])
        self.assertNotIn('history', resumed)
        self.assertNotIn('dynamicTools', resumed)
        self.assertEqual('owner custom\n\n' + WORKING_CONTRACT, resumed['developerInstructions'])
        self.assertEqual({'threadId': 'continuous-owner-thread'}, agent.calls[1][1])
        self.assertNotIn('thread/goal/set', methods)
        turn = agent.calls[2][1]
        self.assertEqual('continuous-owner-thread', turn['threadId'])
        self.assertEqual('plan', turn['collaborationMode']['mode'])
        self.assertIsNone(turn['collaborationMode']['settings']['developer_instructions'])
        self.assertEqual('continuous-owner-thread', result['thread_id'])
        self.assertEqual('fixture-turn', result['turn_id'])
        self.assertEqual(before, checkpoint)

    def test_explicit_model_permissions_and_goal_remain_owner_choices(self):
        goal = {'objective': 'Authorized work', 'status': 'active'}
        agent = self.runtime(instructions='custom', model='owner-chosen-model', model_policy='catalog-first',
                             sandbox='dangerFullAccess', approval_policy='never', goal=goal)
        agent.start('work')
        request = agent.calls[0][1]
        self.assertEqual('owner-chosen-model', request['model'])
        self.assertEqual('dangerFullAccess', request['sandbox'])
        self.assertEqual('never', request['approvalPolicy'])
        self.assertFalse(any(method == 'model/list' for method, params in agent.calls))
        self.assertEqual('turn/start', agent.calls[1][0])
        self.assertEqual('thread/goal/set', agent.calls[2][0])
        self.assertEqual(dict(goal, threadId='fixture-thread'), agent.calls[2][1])
        self.assertEqual(goal, agent.config['goal'])

    def test_contract_does_not_create_goals_or_catalog_scheduler_calls_without_configuration(self):
        agent = self.runtime()
        with patch('assistant_mesh.codex.subprocess.Popen', side_effect=AssertionError('DO_NOT_LAUNCH')), \
                patch('builtins.open', side_effect=AssertionError('DO_NOT_READ_PLAN_OR_AUTH')):
            agent.start('ordinary scoped task')
        self.assertEqual(['thread/start', 'turn/start'], [method for method, params in agent.calls])

    def test_contract_covers_actual_state_frontier_isolation_authority_and_native_escape(self):
        for text in ('goal、plan、task', '真实运行状态', 'docs/PLAN.md', 'docs/TASKS.json',
                     '稳定 ID', '下一步和验收证据', '当前账户访问与额度', '不把模型名或排名固化',
                     '每 3–5 天', 'next_due', '不声称已创建', '零新增费用', '隔离安装', '恢复路径',
                     '不阻断任何原生 Shell', '不要求原生操作先登记', '节点脱网后', '局部自主', '保留 unknown'):
            with self.subTest(text=text):
                self.assertIn(text, WORKING_CONTRACT)

    def test_composition_idempotent_and_marker_in_custom_text_does_not_skip_real_contract(self):
        composed = working_instructions('owner specific')
        self.assertEqual(composed, working_instructions(composed))
        marker_only = '[personal-assistant-mesh 持续工作合同 v1]'
        self.assertEqual(marker_only + '\n\n' + WORKING_CONTRACT, working_instructions(marker_only))

    def test_default_wait_instructions_follow_actual_ready_receipt(self):
        self.assertIn('continue_after_children=true', DEFAULT_INSTRUCTIONS)
        self.assertIn('返回 false 时使用实际 tasks 结果继续当前工作', DEFAULT_INSTRUCTIONS)
        self.assertNotIn('调用 mesh_wait_children 后结束本轮', DEFAULT_INSTRUCTIONS)

    def test_native_goal_tool_hint_does_not_change_model_permissions_or_goal_state(self):
        agent = self.runtime(instructions='keep owner context')
        agent.start('ordinary work')
        instructions = agent.calls[0][1]['developerInstructions']
        self.assertIn('tools.get_goal({})', instructions)
        self.assertIn('tools.update_goal({status:"complete"})', instructions)
        self.assertIn('只有完整目标确实实现才完成 goal', instructions)
        self.assertIn('这些提示不是原生能力门禁', instructions)
        self.assertNotIn('tools.update_goal.enabled', instructions)
        self.assertEqual(['thread/start', 'turn/start'], [method for method, params in agent.calls])
        self.assertNotIn('model', agent.calls[0][1])
        self.assertNotIn('sandbox', agent.calls[0][1])
        self.assertNotIn('approvalPolicy', agent.calls[0][1])

    def test_empty_and_none_owner_prefix_still_receive_contract_without_invented_default(self):
        for value in ('', None):
            with self.subTest(value=value):
                agent = self.runtime(instructions=value)
                agent.start('work')
                self.assertEqual(WORKING_CONTRACT, agent.calls[0][1]['developerInstructions'])

    def test_nontext_instructions_rejected_before_native_request_and_without_echo(self):
        for value in (123, False, ['DO_NOT_PRINT'], {'credential': 'DO_NOT_PRINT'}):
            with self.subTest(value=value):
                agent = self.runtime(instructions=value)
                with self.assertRaisesRegex(CodexError, '^invalid_developer_instructions$'):
                    agent.start('work')
                self.assertEqual([], agent.calls)


if __name__ == '__main__':
    unittest.main()
