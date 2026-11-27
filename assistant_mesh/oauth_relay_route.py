"""Point the existing public OAuth route at a verified, already activated writer."""
import argparse
import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .business_journal import canonical
from .config import private_json, read_secret
from .oauth_writer_activation import OAuthWriterActivation
from .worker import Client, NoRedirect


FORMAT = 'amita-oauth-relay-route/1'
ROUTE = '/_mesh/relay/oauth-route'
METADATA = '/.well-known/oauth-authorization-server'


def _loopback_url(url):
    value = urlsplit(url)
    if (value.scheme != 'http' or value.hostname not in ('127.0.0.1', '::1') or value.port is None
            or value.username is not None or value.path not in ('', '/') or value.query or value.fragment):
        raise ValueError('oauth_relay_private_loopback_required')
    return url.rstrip('/')


def _http(url, headers, proxy=None, body=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy} if proxy else {}), NoRedirect())
    request = urllib.request.Request(url, headers=headers, data=canonical(body).encode() if body is not None else None)
    try:
        with opener.open(request, timeout=6) as response:
            return Client._decode_response(response)
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError('oauth_relay_http_' + str(error.code)) from None


def switch_route(document, activation, controller):
    if (not isinstance(controller, dict) or not {'token_file', 'upstream_origin'} <= set(controller)
            or set(controller) - {'token_file', 'upstream_origin', 'proxy_url'}):
        raise ValueError('oauth_relay_controller_config_invalid')
    target = OAuthWriterActivation(document, activation)
    issuer = target.document['policy']['issuer_url']
    origin = issuer.rstrip('/')
    loopback = _loopback_url(controller['upstream_origin'])
    proxy = _loopback_url(controller['proxy_url']) if controller.get('proxy_url') else None
    token = read_secret(controller['token_file'])
    if re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token) is None:
        raise ValueError('oauth_relay_controller_token_invalid')
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
               'User-Agent': 'AMITA-OAuth-Route/1.0'}
    status = _http(origin + ROUTE, headers, proxy)
    if (not isinstance(status, dict) or status.get('format') != FORMAT or 'current' not in status
            or (status['current'] is not None and not isinstance(status['current'], dict))):
        raise ValueError('oauth_relay_route_status_invalid')
    metadata = _http(loopback + METADATA, {'Host': urlsplit(origin).netloc, 'X-Forwarded-Proto': 'https'})
    if (not isinstance(metadata, dict) or metadata.get('issuer') != issuer
            or metadata.get('authorization_endpoint') != origin + '/authorize'
            or metadata.get('token_endpoint') != origin + '/token'):
        raise ValueError('oauth_relay_live_target_issuer_unconfirmed')
    confirmed = target.confirm_serving_grant()
    receipt = confirmed['receipt']
    grant = dict(group=receipt['group'], scope=receipt['scope'], owner=receipt['grant']['owner'],
                 epoch=receipt['grant']['epoch'], index=receipt['index'], proposal_sha256=receipt['request_sha256'],
                 operation_id=receipt['operation_id'])
    current = status['current']
    if current == grant:
        return dict(format=FORMAT, applied=grant, duplicate=True)
    command = dict(format=FORMAT, grant=grant, previous=current['proposal_sha256'] if current else None,
                   barrier_index=confirmed['barrier']['index'], observed_ms=int(time.time() * 1000))
    outcome = _http(origin + ROUTE, headers, proxy, command)
    if (not isinstance(outcome, dict) or outcome.get('format') != FORMAT or outcome.get('applied') != grant
            or type(outcome.get('duplicate')) is not bool):
        raise ValueError('oauth_relay_original_ack_unconfirmed')
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ('oauth-config', 'activation-config', 'controller-config'):
        parser.add_argument('--' + field, required=True)
    args = parser.parse_args(argv)
    try:
        print(canonical(switch_route(private_json(args.oauth_config), private_json(args.activation_config),
                                     private_json(args.controller_config))))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({'error': str(error)}, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
