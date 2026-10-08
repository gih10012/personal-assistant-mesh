"""Coordination tools supplement, not replace, the model's native toolbox."""
import json


def tool(name, description, properties=None, required=None):
    return {'type': 'function', 'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties or {},
                            'required': required or [], 'additionalProperties': False}}


STRING = {'type': 'string'}
TOOLS = [
    tool('mesh', '统一 mesh 内能力入口，不是本机原生工具门禁或网络代理；不拦截 Shell、文件、网络、MCP 或其他原生能力。'
         'action=discover/describe/graph/audit 查询开放能力目录；advertise/renew/observe/link/revoke/request_grant/authorize 等沿用 resource API，'
         '也可 action=resource, arguments={action:资源动作,arguments:{...}}。kind 与能力描述开放，不要求所有原生工具先登记。'
         'action=remote_delegate 将任务交给 mesh 内已授权 peer，arguments={peer,input,project_id,agent_id,role}；'
         'delegate/children/wait_children 管理原账本子任务，wait_children 后结束本轮以便自动续接；remember/recall/notify 沿用原合同。'
         '身份、task 与 task lease 来自当前认证运行环境，不接受伪造 actor/task_id/task epoch；资源 epoch 仍用于资源版本核对。'
         '目录、authorize.allowed 或 queued 不等于实际执行完成；'
         '没有执行适配器的能力不会通过此入口自动执行。mesh 路线失败不限制 agent 自行使用其他已授权的原生通信路线。',
         {'action': STRING, 'arguments': {'type': 'object', 'additionalProperties': True}}, ['action']),
    tool('mesh_remember', '保存本人偏好或经过验证的工作事实到跨设备持久记忆；不要保存凭据。', {'text': STRING}, ['text']),
    tool('mesh_recall', '查询跨任务记忆。空 query 返回近期记忆。', {'query': STRING}),
    tool('mesh_delegate', '建立可嵌套的持久子任务。模型决定分工与角色；默认任何 agent 节点可领取。',
         {'input': STRING, 'role': STRING, 'project_id': STRING, 'agent_id': STRING, 'required': {'type': 'array', 'items': STRING}}, ['input']),
    tool('mesh_remote_delegate', 'A2A 将工作交给已授权 peer 的自治 agent；不是 SSH 操作对端机器。'
         '使用现有父子任务账本；mesh_children 查看结果，mesh_wait_children 让同一个原生父会话完成后自动继续。'
         '失联仅同一消息 ID 重连；queued 不是完成。peer 必须已通过 owner 的连接/委派授权。',
         {'peer': STRING, 'input': STRING, 'project_id': STRING, 'agent_id': STRING, 'role': STRING}, ['peer', 'input']),
    tool('mesh_children', '查看本任务的子任务状态及实际结果。'),
    tool('mesh_wait_children', '请求在子任务完成后自动继续。调用后结束本轮，不宣称父目标已完成。'),
    tool('mesh_notify', '通过可靠队列主动微信通知本人；queued 或 accepted 都不等于已确认送达。', {'text': STRING}, ['text']),
    tool('mesh_resource', '发现、描述或登记模型创建的工具与动态资源能力；支持算力、性能观测和可达路径。'
         '能力目录不是原生终端白名单。跨节点必须 exact action/scope 授权，模型只能 request_grant，不能自行批准；'
         '声明/health 不等于验证或已执行。actions: discover, describe, graph, audit, advertise, renew, observe, link, revoke, request_grant, authorize。',
         {'action': STRING, 'arguments': {'type': 'object', 'additionalProperties': True}}, ['action']),
]


def result(value, success=True):
    return {'success': success, 'contentItems': [{'type': 'inputText', 'text': json.dumps(value, ensure_ascii=False)}]}
