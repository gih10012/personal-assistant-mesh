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
         'action=allocation, arguments={action:reserve/inspect/pending/accept/start/unknown/settle/decline/cancel,arguments:{...}}，'
         '模型可选择有新鲜证据和精确授权的能力/路径并原子预留owner绑定的共享容量；reserve的task_id/task_epoch由运行环境注入，不要提供。'
         'provider回执不是工具执行证明，未接执行适配器不会自动执行；未知占用不能因超时直接释放或新ID重做。'
         'action=runtime_diagnose, arguments={} 只读观察本worker配置绑定的本节点运行包布局，不接受任意path/config，'
         '不读凭据、不联网推理、不安装/切换/重试；未知布局不是能力禁止，complete不证明原生Shell已验证。'
         'action=remote_delegate 将任务交给 mesh 内已授权 peer，arguments={peer,input,project_id,agent_id,role}；'
         'delegate/children/wait_children 管理原账本子任务；wait_children 返回 continue_after_children=true 才结束本轮等待续接，'
         '返回 false 时使用 tasks 中的实际结果继续当前工作，不再等待；remember/recall/notify 沿用原合同。'
         '身份、task 与 task lease 来自当前认证运行环境，不接受伪造 actor/task_id/task epoch；资源 epoch 仍用于资源版本核对。'
         '目录、authorize.allowed 或 queued 不等于实际执行完成；'
         'action=federated_capabilities,arguments={issuer,kind,include_unavailable,limit} 查询可选远端能力投影，'
         '是带issuer/revision的声明证据，不导入授权或容量；远端执行仍通过remote_delegate由owner authority核对。'
         'action=routing_context,arguments={kind,issuer,include_unavailable,limit,observation_max_age_seconds} 读取任务绑定证据快照；无固定排序/评分，不探网或授权。'
         'route_propose,arguments={decision:{decision_id,work,candidates,selection,rationale,evidence_refs}} 保存不可变提议；refs仍为模型引用，未自动核验。'
         'route_inspect{decision_id}/route_list{limit}/route_link{decision_id,kind,reference_id}；link的kind为remote_delegation或managed_allocation，'
         '仅核对实际账本原执行身份，不证明模型选择、计划对齐或业务成功。unknown必须查原ID，不新建ID重做。'
         '没有执行适配器的能力不会通过此入口自动执行。mesh 路线失败不限制 agent 自行使用其他已授权的原生通信路线。',
         {'action': STRING, 'arguments': {'type': 'object', 'additionalProperties': True}}, ['action']),
    tool('mesh_remember', '保存本人偏好或经过验证的工作事实到跨设备持久记忆；不要保存凭据。', {'text': STRING}, ['text']),
    tool('mesh_recall', '查询跨任务记忆。空 query 返回近期记忆。', {'query': STRING}),
    tool('mesh_delegate', '建立可嵌套的持久子任务。模型决定分工与角色；默认任何 agent 节点可领取。',
         {'input': STRING, 'role': STRING, 'project_id': STRING, 'agent_id': STRING, 'required': {'type': 'array', 'items': STRING}}, ['input']),
    tool('mesh_remote_delegate', 'A2A 将工作交给已授权 peer 的自治 agent；不是 SSH 操作对端机器。'
         '使用现有父子任务账本；mesh_children 查看结果，mesh_wait_children 按实际 readiness 等待并让同一个原生父会话自动继续，'
         '已结束时直接返回结果供当前 turn 使用。'
         '失联仅同一消息 ID 重连；queued 不是完成。peer 必须已通过 owner 的连接/委派授权。',
         {'peer': STRING, 'input': STRING, 'project_id': STRING, 'agent_id': STRING, 'role': STRING}, ['peer', 'input']),
    tool('mesh_children', '查看本任务的子任务状态及实际结果。'),
    tool('mesh_wait_children', '读取实际子任务状态与结果；只有返回 continue_after_children=true 才结束本轮等待自动续接。'
         '返回 false 时使用 tasks 中的结果继续当前工作，不再次等待；failed/needs_review 不代表成功，不宣称父目标已完成。'),
    tool('mesh_notify', '显式通知本人：使用配置的本人通道或节点回传链路；private 模式没有回传时仅 recorded_private。queued 或 accepted 都不等于手机已确认送达。', {'text': STRING}, ['text']),
    tool('mesh_resource', '发现、描述或登记模型创建的工具与动态资源能力；支持算力、性能观测和可达路径。'
         '能力目录不是原生终端白名单。跨节点必须 exact action/scope 授权，模型只能 request_grant，不能自行批准；'
         '声明/health 不等于验证或已执行。actions: discover, describe, graph, audit, advertise, renew, observe, link, revoke, request_grant, authorize。',
         {'action': STRING, 'arguments': {'type': 'object', 'additionalProperties': True}}, ['action']),
]


def result(value, success=True):
    return {'success': success, 'contentItems': [{'type': 'inputText', 'text': json.dumps(value, ensure_ascii=False)}]}
