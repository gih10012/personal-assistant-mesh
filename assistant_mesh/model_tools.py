"""Coordination tools supplement, not replace, the model's native toolbox."""
import json


def tool(name, description, properties=None, required=None):
    return {'type': 'function', 'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties or {},
                            'required': required or [], 'additionalProperties': False}}


STRING = {'type': 'string'}
TOOLS = [
    tool('mesh_remember', '保存本人偏好或经过验证的工作事实到跨设备持久记忆；不要保存凭据。', {'text': STRING}, ['text']),
    tool('mesh_recall', '查询跨任务记忆。空 query 返回近期记忆。', {'query': STRING}),
    tool('mesh_delegate', '建立可嵌套的持久子任务。模型决定分工与角色；默认任何 agent 节点可领取。',
         {'input': STRING, 'role': STRING, 'project_id': STRING, 'agent_id': STRING, 'required': {'type': 'array', 'items': STRING}}, ['input']),
    tool('mesh_children', '查看本任务的子任务状态及实际结果。'),
    tool('mesh_wait_children', '请求在子任务完成后自动继续。调用后结束本轮，不宣称父目标已完成。'),
    tool('mesh_notify', '通过可靠队列主动微信通知本人；queued 或 accepted 都不等于已确认送达。', {'text': STRING}, ['text']),
]


def result(value, success=True):
    return {'success': success, 'contentItems': [{'type': 'inputText', 'text': json.dumps(value, ensure_ascii=False)}]}
