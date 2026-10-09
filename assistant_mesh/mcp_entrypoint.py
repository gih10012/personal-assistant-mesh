"""Thin legacy MCP stdio entrance to the existing owner-bound Mesh contract.

This process adds three managed capabilities, not a native tool sandbox. It
does not enroll a worker, discover credentials, approve results, run a model,
or retry a failed request. Caller-owned request IDs outlive JSON-RPC sessions.
"""
import json
import sys

from . import native_messaging as native


PROTOCOL_VERSION = '2025-06-18'
SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION,)
VERSION = '0.1.0'
MAX_LINE_BYTES = 32768
MAX_RESPONSE_BYTES = 65536
_EOF = object()
_ACTIONS = {'mesh_submit_task': 'submit', 'mesh_task_status': 'status',
            'mesh_approved_result': 'result'}
INSTRUCTIONS = (
    'These tools are extra managed Mesh capabilities; native Shell, files, '
    'network and other MCP tools remain available. Persist an original '
    'request_id before submitting. Status and approved result use that same '
    'ID. Unknown outcomes are not evidence of failure: do not invent a new ID '
    'or replay automatically; inspect the original request. Project/agent '
    'labels are requests, not authorization or native-session selection. '
    'A completed status is not proof of execution or independently verified '
    'artifact content. Only explicitly owner-approved results are returned.'
)


class ProtocolError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message


def _schema(submit=False):
    properties = {'request_id': {'type': 'string', 'minLength': 1, 'maxLength': 200,
                                'description': 'Original durable caller-owned request ID; never replace after unknown outcome.'}}
    required = ['request_id']
    if submit:
        properties.update({
            'input': {'type': 'string', 'minLength': 1, 'maxLength': native.MAX_TASK_TEXT,
                      'description': 'Owner-authorized task text, at most 16384 UTF-8 bytes.'},
            'project_id': {'type': 'string', 'minLength': 1, 'maxLength': 200,
                           'description': 'Optional model-readable project label, not authorization.'},
            'agent_id': {'type': 'string', 'minLength': 1, 'maxLength': 200,
                         'description': 'Optional requested-agent label, not worker/native-context selection.'}})
        required.append('input')
    return {'type': 'object', 'properties': properties, 'required': required,
            'additionalProperties': False}


def tools():
    descriptions = {
        'mesh_submit_task': 'Submit one owner-authorized task to the managed Mesh using a retained original request_id. No automatic retry.',
        'mesh_task_status': 'Read metadata-only status of this entrance identity\'s original request; no raw result/history or global task lookup.',
        'mesh_approved_result': 'Read only the original request\'s immutable owner-approved summary/public references, never raw native output.'}
    return [{'name': name, 'description': descriptions[name],
             'inputSchema': _schema(action == 'submit'),
             'annotations': {'readOnlyHint': action != 'submit',
                             'destructiveHint': action == 'submit', 'idempotentHint': True,
                             'openWorldHint': action == 'submit'}}
            for name, action in sorted(_ACTIONS.items())]


def _error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id,
            'error': {'code': code, 'message': message}}


def _object(value, allowed, required=()):
    if (not isinstance(value, dict) or set(value) - set(allowed)
            or not set(required) <= set(value)):
        raise ProtocolError(-32602, 'Invalid params')
    if '_meta' in value and not isinstance(value['_meta'], dict):
        raise ProtocolError(-32602, 'Invalid params')
    return value


def _request_id(value):
    if (type(value) is int and -(2 ** 63) <= value < 2 ** 63
            or isinstance(value, str) and 1 <= len(value.encode('utf8')) <= 200):
        return value
    raise ProtocolError(-32600, 'Invalid Request')


class Session:
    """One stdin session; no business retry/cache/background control loop."""
    def __init__(self, client):
        self.client = client
        self.negotiated = False
        self.ready = False

    def handle(self, value):
        request_id, notification = None, False
        try:
            if (not isinstance(value, dict) or value.get('jsonrpc') != '2.0'
                    or set(value) - {'jsonrpc', 'id', 'method', 'params'}
                    or not isinstance(value.get('method'), str)):
                raise ProtocolError(-32600, 'Invalid Request')
            notification = 'id' not in value
            if not notification:
                request_id = _request_id(value['id'])
            method, params = value['method'], value.get('params', {})
            if notification:
                # Notifications must never submit tasks. Cancellation does
                # not revoke an already committed Mesh task or cause replay.
                if method == 'notifications/initialized' and self.negotiated:
                    _object(params, {'_meta'})
                    self.ready = True
                return None
            if method == 'ping':
                _object(params, {'_meta'})
                result = {}
            elif method == 'initialize':
                if self.negotiated:
                    raise ProtocolError(-32600, 'Already initialized')
                _object(params, {'protocolVersion', 'capabilities', 'clientInfo', '_meta'},
                        {'protocolVersion', 'capabilities', 'clientInfo'})
                if (not isinstance(params['protocolVersion'], str)
                        or not 1 <= len(params['protocolVersion']) <= 32
                        or not isinstance(params['capabilities'], dict)):
                    raise ProtocolError(-32602, 'Invalid params')
                info = params['clientInfo']
                if (not isinstance(info, dict) or not all(isinstance(info.get(key), str)
                        and 1 <= len(info[key]) <= 200 for key in ('name', 'version'))):
                    raise ProtocolError(-32602, 'Invalid params')
                # MCP legacy negotiation returns the server's actual supported
                # version when the proposal differs. A client unable to use
                # it must disconnect; this is NOT support for modern lifecycle.
                result = {'protocolVersion': PROTOCOL_VERSION,
                          'capabilities': {'tools': {'listChanged': False}},
                          'serverInfo': {'name': 'personal-assistant-mesh', 'version': VERSION},
                          'instructions': INSTRUCTIONS}
                self.negotiated = True
            elif not self.ready:
                raise ProtocolError(-32000, 'Server not initialized')
            elif method == 'tools/list':
                _object(params, {'_meta'})
                result = {'tools': tools()}
            elif method == 'tools/call':
                _object(params, {'name', 'arguments', '_meta'}, {'name', 'arguments'})
                name = params['name']
                if not isinstance(name, str) or name not in _ACTIONS:
                    raise ProtocolError(-32602, 'Unknown tool')
                action, arguments = _ACTIONS[name], params['arguments']
                try:
                    native.validate_message({'schema': 1, 'action': action, 'payload': arguments})
                    if action == 'submit' and any(key in arguments and not isinstance(arguments[key], str)
                                                  for key in ('project_id', 'agent_id')):
                        raise ValueError('label')
                except Exception:
                    raise ProtocolError(-32602, 'Invalid tool arguments') from None
                # Native bridge uses the same Ingress validators, fixed Client
                # routes and exact closed projection. One call, zero retries.
                data = native.dispatch(self.client, {'schema': 1, 'action': action,
                                                     'payload': arguments})
                result = {'content': [{'type': 'text', 'text': native._json_bytes(data).decode('utf8')}],
                          'structuredContent': data, 'isError': not data['ok']}
            else:
                raise ProtocolError(-32601, 'Method not found')
            return {'jsonrpc': '2.0', 'id': request_id, 'result': result}
        except ProtocolError as error:
            return None if notification else _error(request_id, error.code, error.message)
        except Exception:
            # Never echo exception strings, paths, native output or credentials.
            return None if notification else _error(request_id, -32603, 'Internal error')


def read_line(stream):
    raw = stream.readline(MAX_LINE_BYTES + 1)
    if raw == b'':
        return _EOF
    if not isinstance(raw, bytes) or len(raw) > MAX_LINE_BYTES or not raw.endswith(b'\n'):
        raise ProtocolError(-32700, 'Invalid or oversized JSON-RPC line')
    try:
        value = json.loads(raw.decode('utf8'), object_pairs_hook=native._unique,
                           parse_constant=native._nonfinite)
        native._json_bytes(value)
        return value
    except Exception:
        raise ProtocolError(-32700, 'Parse error') from None


def write_line(stream, value):
    raw = native._json_bytes(value)
    if len(raw) + 1 > MAX_RESPONSE_BYTES:
        raise ProtocolError(-32603, 'Response too large')
    remaining = memoryview(raw + b'\n')
    while remaining:
        count = stream.write(remaining)
        if type(count) is not int or not 1 <= count <= len(remaining):
            raise ProtocolError(-32603, 'Output unavailable')
        remaining = remaining[count:]
    stream.flush()


def run(input_stream, output_stream, client):
    session = Session(client)
    while True:
        try:
            value = read_line(input_stream)
        except ProtocolError as error:
            write_line(output_stream, _error(None, error.code, error.message))
            # Do not try to resynchronize partial/oversized transport frames.
            return 1
        if value is _EOF:
            return 0
        response = session.handle(value)
        if response is not None:
            write_line(output_stream, response)


def main(config_path=None, input_stream=None, output_stream=None):
    """An owner-installed launcher supplies private config, never tool input."""
    incoming = sys.stdin.buffer if input_stream is None else input_stream
    outgoing = sys.stdout.buffer if output_stream is None else output_stream
    try:
        # No credential-free tool execution or fallback to operator config.
        client = native.client_from_config(config_path)
        return run(incoming, outgoing, client)
    except Exception:
        # Fixed stderr diagnostic only. stdout stays a JSON-RPC-only channel.
        sys.stderr.write('Mesh MCP unavailable\n')
        return 1
