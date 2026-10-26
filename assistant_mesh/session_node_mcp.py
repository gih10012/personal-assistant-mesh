"""Five real SDK tool registrations, shared by FastMCP/MCPServer factories.

No SDK dependency is imported until the owning HTTP factory supplies its real
server and ToolAnnotations class. The core remains usable on Python >=3.8.
"""
from typing import Any, Dict, List


INSTRUCTIONS = (
    ' When AMITA/network capabilities are needed, connect this interactive Chat '
    'with a retained session_id/control_id, then discover the trusted owner-bound '
    'network; no business task is needed for discovery/join. Announce only tools '
    'and capabilities actually available in this session. Retry a lost connect '
    'or node_update with the same control ID. Renew this interactive membership '
    'while actively using Mesh; let it expire or mark offline when idle. '
    'Use mesh_work_pull only for eligible, independently authorized work, and '
    'retain every returned task/request/conversation/epoch/operation identity. '
    'Execute only while may_execute and lease_live are true. Report effect_started '
    'before an external effect, and report progress/results with one retained '
    'report_id. A lost claim/report ACK only reconciles that same ID. An unknown '
    'external effect requires the original operation receipt, never replay. '
    'Use local native Chat tools for suitable work and A2A/capability collaboration '
    'for other devices; Mesh grants do not restrict native tools. Network, agent, '
    'model and business-task states are separate. Interactive registration proves '
    'neither provider execution nor a daemon. Ordinary Chat has no arbitrary '
    'background wake; MCP Events apply only on hosts that actually support them.'
)


def register_tools(server, invoke, annotations):
    """Install actual callable tools on the supplied SDK server, not descriptors."""
    def hints(read=False):
        return annotations(read_only_hint=read, destructive_hint=False,
                           idempotent_hint=True, open_world_hint=False)

    @server.tool(name='mesh_connect', annotations=hints(),
                 description='Connect/resume this owner-bound interactive Chat node using one retained control_id; safely retry that same handshake after a lost ACK. No business task, provider attestation or background daemon is created.')
    async def connect(session_id: str, control_id: str, capabilities: List[str] = None,
                      agent_state: str = 'ready', model_state: str = 'unknown'):
        return await invoke('connect', {'session_id': session_id, 'control_id': control_id,
            'capabilities': capabilities or [], 'agent_state': agent_state, 'model_state': model_state})

    @server.tool(name='mesh_discover', annotations=hints(True),
                 description='Discover trusted AMITA nodes, roles and capabilities without creating a task or requiring a running model/Leader turn. Network, agent, model and task observations remain separate. Optional retained session_id reads that membership.')
    async def discover(session_id: str = None):
        return await invoke('discover', {'session_id': session_id} if session_id is not None else {})

    @server.tool(name='mesh_node_update', annotations=hints(),
                 description='Publish this interactive session actual capabilities and online state with a retained control_id. This is a complete snapshot. Advertising capabilities never grants managed work authority and never restricts native Chat tools.')
    async def node_update(session_id: str, control_id: str, capabilities: List[str] = None,
                          online: bool = True, agent_state: str = 'ready', model_state: str = 'unknown'):
        return await invoke('node_update', {'session_id': session_id, 'control_id': control_id,
            'capabilities': capabilities or [], 'online': online,
            'agent_state': agent_state, 'model_state': model_state})

    @server.tool(name='mesh_work_pull', annotations=hints(),
                 description='Atomically claim only work qualified by current session capabilities and a separate owner-installed grant. Retain pull_id after a lost ACK to observe its original task/epoch/operation. No fake keeper tasks or business-task copies.')
    async def work_pull(session_id: str, pull_id: str):
        return await invoke('work_pull', {'session_id': session_id, 'pull_id': pull_id})

    @server.tool(name='mesh_work_report', annotations=hints(),
                 description='Report the original claimed task/request/conversation/epoch/operation through the real authority. status: progress, effect_started, unknown, completed, failed, waiting_auth, waiting_backend. Retry the same report_id only to reconcile its receipt. Completing an announced effect requires its original effect_receipt {operation_id, reference}; reports are not independent execution proof.')
    async def work_report(session_id: str, report_id: str, task_id: str, epoch: int,
                          operation_id: str, request_id: str, status: str,
                          conversation_id: str = None, checkpoint: Dict[str, Any] = None,
                          result: str = None, effect_receipt: Dict[str, str] = None):
        return await invoke('work_report', {'session_id': session_id, 'report_id': report_id,
            'task_id': task_id, 'epoch': epoch, 'operation_id': operation_id, 'request_id': request_id,
            'conversation_id': conversation_id, 'status': status, 'checkpoint': checkpoint or {},
            'result': result, 'effect_receipt': effect_receipt})
