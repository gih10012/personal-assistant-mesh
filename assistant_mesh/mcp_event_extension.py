"""SDK 2.3 adapter for the draft webhook methods, not a new MCP endpoint."""


def make_extension(service, verifier, get_access_token):
    import asyncio
    from typing import Any, Literal
    from mcp.server.extension import Extension, MethodBinding
    from mcp.shared.exceptions import MCPError
    from mcp_types import RequestParams
    from pydantic import ConfigDict
    from .mcp_event_transport import CallbackError
    from .store import Conflict

    class StrictParams(RequestParams):
        model_config = ConfigDict(extra='forbid', strict=True)

    class ListParams(StrictParams):
        cursor: None = None

    class SubscribeParams(StrictParams):
        name: Literal['task.completed', 'leader.attention_required']
        arguments: dict[str, Any]
        delivery: dict[str, Any]
        cursor: None = None
        ttlMs: int | None = 3600000

    class UnsubscribeParams(StrictParams):
        name: Literal['task.completed', 'leader.attention_required']
        arguments: dict[str, Any]
        delivery: dict[str, Any]

    def handler(action):
        async def call(context, params):
            token = get_access_token()
            try:
                principal = verifier.principal(token)
                value = params.model_dump(by_alias=True, exclude={'meta'})
                result = await asyncio.to_thread(getattr(service, action), principal, **value)
                verifier.principal(token)
                return result
            except PermissionError:
                raise MCPError(code=-32001, message='Event not authorized or not visible') from None
            except CallbackError as error:
                raise MCPError(code=-32015, message='CallbackEndpointError', data={'reason': error.reason}) from None
            except Conflict:
                raise MCPError(code=-32000, message='Original event subscription changed') from None
            except ValueError:
                raise MCPError(code=-32602, message='Invalid event parameters') from None
            except Exception:
                raise MCPError(code=-32603, message='Event service unavailable') from None
        return call

    class WorkEvents(Extension):
        identifier = 'org.personal-assistant-mesh/events'

        def methods(self):
            version = frozenset(('2026-07-28',))
            return (MethodBinding('events/list', ListParams, handler('list'), version),
                    MethodBinding('events/subscribe', SubscribeParams, handler('subscribe'), version),
                    MethodBinding('events/unsubscribe', UnsubscribeParams, handler('unsubscribe'), version))

    return WorkEvents()
