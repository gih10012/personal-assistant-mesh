"""Render an optional DNS vhost beside the EXISTING IP HTTPS deployment.

Source/argv only: no DNS, Docker, CA, account, key, listener or service operation.
Absent/null dns_profile returns every original IP asset byte-for-byte.
"""
import argparse
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re


_SPEC = importlib.util.spec_from_file_location('_hostname_original_ip_tls',
    str(Path(__file__).resolve().with_name('prepare-ip-tls.py')))
ip_tls = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ip_tls)
DNS_CERT_NAME = 'mesh-dns'


def validate(document):
    if not isinstance(document, dict):
        raise ValueError('hostname_tls_owner_config_required')
    original = dict(document)
    profile = original.pop('dns_profile', None)
    original = ip_tls.validate(original)
    if profile is None:
        return original, None
    if not isinstance(profile, dict) or set(profile) != {'hostname', 'mcp_port', 'cloud_port', 'oauth_port', 'accounts'}:
        raise ValueError('hostname_tls_profile_required')
    host = profile['hostname']
    label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    if (not isinstance(host, str) or not 3 <= len(host) <= 253
            or not re.fullmatch(label + r'(?:\.' + label + r')+', host)
            or not re.search('[a-z]', host.rsplit('.', 1)[-1])):
        raise ValueError('hostname_tls_canonical_fqdn_required')
    accounts = profile['accounts']
    if (not isinstance(accounts, dict) or set(accounts) != {'staging', 'production'}
            or any(not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value)
                   for value in accounts.values())):
        raise ValueError('hostname_tls_existing_acme_account_ids_required')
    # Reuse the original dedicated-port checks and same-ASGI OAuth constraint.
    ports = dict(original)
    ports.update({key: profile[key] for key in ('mcp_port', 'cloud_port', 'oauth_port')})
    ip_tls.validate(ports)
    occupied = {original.get(key) for key in ('mcp_port', 'cloud_port', 'oauth_port')} - {None}
    if occupied & {profile[key] for key in ('mcp_port', 'cloud_port', 'oauth_port')}:
        raise ValueError('hostname_tls_separate_sidecars_required')
    return original, dict(profile, accounts=dict(accounts))


def _profile_config(original, profile):
    # The original narrow route renderer uses public_ip as its fixed Host.
    # This dict is owner code, after strict FQDN and original port validation.
    return dict(original, public_ip=profile['hostname'],
        **{key: profile[key] for key in ('mcp_port', 'cloud_port', 'oauth_port')})


def _dns_servers(original, profile, tls=False):
    host = profile['hostname']
    config = _profile_config(original, profile)
    maps = '''    map $http_host $mesh_dns_host80 {
        default 0;
        "__HOST__" 1;
        "__HOST__:80" 1;
    }
    map $http_host $mesh_dns_host443 {
        default 0;
        "__HOST__" 1;
        "__HOST__:443" 1;
    }
'''.replace('__HOST__', host)
    oauth = profile['oauth_port'] is not None
    if oauth and original.get('oauth_port') is None:
        maps += ip_tls.oauth_maps()
    servers = '''    server {
        listen 80;
        server_name __HOST__;
        if ($mesh_dns_host80 = 0) { return 404; }
        if ($request_uri != $uri) { return 404; }
        location ~ "^/\\.well-known/acme-challenge/[A-Za-z0-9_-]{20,200}$" {
            if ($request_method != GET) { return 405; }
            if ($http_content_length !~ "^(0)?$") { return 413; }
            root /mesh-webroot;
            default_type text/plain;
            try_files $uri =404;
        }
        location / { return 404; }
    }
'''.replace('__HOST__', host)
    if tls:
        locations = [ip_tls.route(config, '/mcp', profile['mcp_port'], ('POST', 'GET', 'DELETE'), 65536),
            ip_tls.route(config, '/cloud/node', profile['cloud_port'], ('POST',), 8 * 1024 * 1024),
            ip_tls.route(config, '/.well-known/oauth-protected-resource/mcp', profile['mcp_port'],
                         ('GET',), 1024, metadata=True)]
        if oauth:
            locations += [
                ip_tls.oauth_route(config, ip_tls.OAUTH_PATHS[0], ('GET', 'HEAD', 'OPTIONS')),
                ip_tls.oauth_route(config, '/authorize', ('GET', 'HEAD', 'POST'), form=True),
                ip_tls.oauth_route(config, '/token', ('POST', 'OPTIONS'), form=True),
                ip_tls.oauth_route(config, '/register', ('POST', 'OPTIONS'), registration=True),
                ip_tls.oauth_route(config, '/auth/callback', ('GET', 'HEAD')),
                ip_tls.oauth_route(config, '/consent', ('GET', 'HEAD', 'POST'), form=True)]
        server = '''    server {
        listen 443 ssl;
        server_name __HOST__;
        ssl_certificate /etc/letsencrypt-dns/live/mesh-dns/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt-dns/live/mesh-dns/privkey.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_tickets off;
        if ($mesh_dns_host443 = 0) { return 404; }
        if ($request_uri != $uri) { return 404; }
        if ($http_transfer_encoding != "") { return 400; }
        if ($http_content_encoding != "") { return 400; }
        if ($http_expect != "") { return 417; }
        if ($http_upgrade != "") { return 400; }
        if ($http_cookie != "") { return 400; }
__LOCATIONS__
        location / { return 404; }
    }
'''.replace('__HOST__', host).replace('__LOCATIONS__', '\n'.join(locations))
        if oauth:
            server = server.replace('if ($request_uri != $uri) { return 404; }',
                                    'if ($mesh_tls_target = 0) { return 404; }')
            server = server.replace('if ($http_cookie != "") { return 400; }',
                                    'if ($mesh_tls_cookie = 0) { return 400; }')
        servers += server
    return maps, servers


def nginx(original, profile, ip_https=True, dns_https=True):
    text = ip_tls.nginx(original, tls=ip_https)
    maps, servers = _dns_servers(original, profile, tls=dns_https)
    if not text.endswith('}\n') or '    server {' not in text:
        raise ValueError('hostname_tls_original_renderer_changed')
    text = text.replace('    server {', maps + '    server {', 1)
    return text[:-2] + servers + '}\n'


def gateway_command(original, test=False):
    command = ip_tls.gateway_command(original, test=test)
    index = command.index('--entrypoint')
    root = original['runtime_root'] + '/letsencrypt-dns-production'
    mounts = ip_tls.mount(root + '/live', '/etc/letsencrypt-dns/live', True)
    mounts += ip_tls.mount(root + '/archive', '/etc/letsencrypt-dns/archive', True)
    return command[:index] + mounts + command[index:]


def certbot_command(original, profile, mode, renew=False, dry_run=False):
    if mode not in ('staging', 'production') or (dry_run and not renew):
        raise ValueError('hostname_tls_certificate_mode_invalid')
    root = original['runtime_root']
    command = ip_tls.docker_base(original, 'mesh-dns-acme-' + mode, '256m')
    for source, target in (('letsencrypt-dns-' + mode, '/etc/letsencrypt'),
            ('work-dns-' + mode, '/var/lib/letsencrypt'),
            ('logs-dns-' + mode, '/var/log/letsencrypt'), ('webroot', '/mesh-webroot')):
        command += ip_tls.mount(root + '/' + source, target)
    # Reuse the existing ACME account read-only. A missing account fails closed;
    # no DNS profile command can register another one or overwrite IP state.
    account_mode = 'staging' if dry_run else mode
    command += ip_tls.mount(root + '/letsencrypt-' + account_mode + '/accounts', '/etc/letsencrypt/accounts', True)
    command += ip_tls.mount(root + '/config', '/mesh-hooks', True)
    command += [ip_tls.CERTBOT, 'renew' if renew else 'certonly', '--non-interactive',
        '--config-dir', '/etc/letsencrypt', '--work-dir', '/var/lib/letsencrypt',
        '--logs-dir', '/var/log/letsencrypt', '--cert-name', DNS_CERT_NAME,
        '--account', profile['accounts'][account_mode],
        '--server', ip_tls.STAGING if mode == 'staging' or dry_run else ip_tls.PRODUCTION,
        '--preferred-challenges', 'http', '--max-log-backups', '3', '--no-directory-hooks']
    if renew:
        command += ['--no-random-sleep-on-renew']
        if dry_run:
            command += ['--dry-run']
    else:
        command += ['--webroot', '--webroot-path', '/mesh-webroot', '-d', profile['hostname'],
                    '--keep-until-expiring']
    if mode == 'production':
        command += ['--deploy-hook', '/mesh-hooks/deploy-hook-dns.sh']
    return command


def render(document):
    original, profile = validate(document)
    assets = ip_tls.render(original)
    if profile is None:
        return assets
    root = original['runtime_root']
    assets['nginx-http.conf'] = nginx(original, profile, ip_https=False, dns_https=False)
    assets['nginx-dns-bootstrap.conf'] = nginx(original, profile, dns_https=False)
    assets['nginx-tls.conf'] = nginx(original, profile)
    # This addon targets an already serving IP deployment. DNS bootstrap keeps
    # its 443 vhost while adding only the new name's HTTP-01 challenge route.
    assets['nginx-current.conf'] = assets['nginx-dns-bootstrap.conf']
    assets['deploy-hook-dns.sh'] = '''#!/bin/sh
set -eu
umask 077
test "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/mesh-dns
printf '%s\\n' mesh-dns > /var/lib/letsencrypt/mesh-dns-reload-required
'''
    docker = ['/usr/bin/docker', '--config', root + '/docker-client']
    reloads = '\n'.join(ip_tls.shell(docker + ['exec', ip_tls.CONTAINER] + args)
        for args in (['nginx', '-t', '-c', '/mesh-config/nginx-current.conf'],
                     ['nginx', '-s', 'reload', '-c', '/mesh-config/nginx-current.conf']))
    marker = root + '/work-dns-production/mesh-dns-reload-required'
    assets['renew-dns.sh'] = '''#!/bin/sh
set -eu
umask 077
__RENEW__
if test -f __MARKER__; then
    test "$(/usr/bin/stat -c %u:%a __MARKER__)" = 0:600
    test "$(/usr/bin/head -n 1 __MARKER__)" = mesh-dns
__RELOAD__
    /usr/bin/unlink __MARKER__
fi
'''.replace('__RENEW__', ip_tls.shell(certbot_command(original, profile, 'production', renew=True)))
    assets['renew-dns.sh'] = assets['renew-dns.sh'].replace('__MARKER__', marker).replace('__RELOAD__', reloads)
    assets['personal-assistant-mesh-dns-renew.service'] = assets['personal-assistant-mesh-ip-renew.service'].replace(
        'dedicated Mesh short-lived IP certificate', 'dedicated Mesh DNS certificate').replace(
        '/bin/renew.sh', '/bin/renew-dns.sh')
    assets['personal-assistant-mesh-dns-renew.timer'] = assets['personal-assistant-mesh-ip-renew.timer'].replace(
        'dedicated Mesh IP certificate', 'dedicated Mesh DNS certificate').replace(
        'personal-assistant-mesh-ip-renew.service', 'personal-assistant-mesh-dns-renew.service')
    assets['personal-assistant-mesh-ip-tls.service'] = assets['personal-assistant-mesh-ip-tls.service'].replace(
        ip_tls.shell(ip_tls.gateway_command(original)), ip_tls.shell(gateway_command(original)))
    plan = json.loads(assets['plan.json'])
    plan['commands']['create_private_directories'] += [root + '/' + item + '-' + mode
        for mode in ('staging', 'production')
        for item in ('letsencrypt-dns', 'work-dns', 'logs-dns')]
    plan['commands']['create_private_directories'] += [root + '/letsencrypt-dns-production/' + item
                                                    for item in ('live', 'archive')]
    plan['commands']['test_gateway_current_config'] = gateway_command(original, test=True)
    plan['commands']['run_gateway_foreground'] = gateway_command(original)
    plan['commands'].update({
        'dns_issue_staging': certbot_command(original, profile, 'staging'),
        'dns_issue_production': certbot_command(original, profile, 'production'),
        'dns_dry_run_production_renewal': certbot_command(original, profile, 'production', renew=True, dry_run=True)})
    plan['dns_profile'] = dict(profile, certificate_name=DNS_CERT_NAME,
        required_tls_container_recreate=True, acme_accounts_reused_readonly=True,
        bootstrap_config='nginx-dns-bootstrap.conf', active_config='nginx-tls.conf',
        rollback_config='nginx-dns-bootstrap.conf', new_account_registration=False)
    assets['plan.json'] = json.dumps(plan, indent=2, sort_keys=True) + '\n'
    return assets


def build(document, destination):
    assets = render(document)
    destination = Path(destination)
    if (not destination.is_absolute() or '..' in destination.parts or destination.exists()
            or any(parent.is_symlink() for parent in destination.parents)):
        raise ValueError('hostname_tls_new_absolute_destination_required')
    destination.mkdir(mode=0o700)
    manifest = {}
    for name, text in assets.items():
        descriptor = os.open(str(destination / name), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o700 if name.endswith('.sh') else 0o600)
        raw = text.encode('utf8')
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
        manifest[name] = hashlib.sha256(raw).hexdigest()
    return {'rendered_only': True, 'files': manifest, 'network_or_deploy_performed': False,
            'account_or_key_created': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-config', required=True)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    from assistant_mesh.config import private_json
    print(json.dumps(build(private_json(args.owner_config), args.destination), sort_keys=True))


if __name__ == '__main__':
    main()
