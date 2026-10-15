"""Render NEW owner-reviewed nginx/Certbot IP-TLS assets; never deploy or issue.

Python 3.6 compatible. No network, Docker, CA/account/key, firewall, service or
proxy mutation. The operations owner installs/reviews these new files and runs
the explicit plan. Only dedicated authenticated sidecars may be selected.
"""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
from pathlib import Path


CERTBOT = 'certbot/certbot@sha256:398c47284a6d6782825be71685f677ef3a1e65b8b5c278a8b1e99f6da84b4eb9'
NGINX = 'nginx@sha256:959ae6b898852db82e5b29b929388e59914f42be0a8f4d44c89a4c1f53b49208'
PRODUCTION = 'https://acme-v02.api.letsencrypt.org/directory'
STAGING = 'https://acme-staging-v02.api.letsencrypt.org/directory'
CONTAINER = 'personal-assistant-mesh-ip-tls'
CERT_NAME = 'mesh-ip'


def validate(config):
    if (not isinstance(config, dict) or set(config) !=
            {'public_ip', 'runtime_root', 'mcp_port', 'cloud_port'}):
        raise ValueError('ip_tls_owner_config_invalid')
    address = ipaddress.ip_address(config['public_ip'])
    if address.version != 4 or not address.is_global or str(address) != config['public_ip']:
        raise ValueError('ip_tls_public_ipv4_required')
    root = config['runtime_root']
    if (not isinstance(root, str) or not re.fullmatch(r'/opt/[a-zA-Z0-9_-]+', root)
            or len(root) > 120):
        raise ValueError('ip_tls_dedicated_opt_root_required')
    for name in ('mcp_port', 'cloud_port'):
        port = config[name]
        if port is not None and (type(port) is not int or not 1024 <= port <= 65535
                                 or port in (2053, 2096, 17680, 17681)):
            raise ValueError('ip_tls_dedicated_sidecar_port_required')
    if config['mcp_port'] is not None and config['mcp_port'] == config['cloud_port']:
        raise ValueError('ip_tls_separate_sidecars_required')
    return dict(config)


def shell(argv):
    return ' '.join(shlex.quote(str(a)) for a in argv)


def forwarding(ip):
    # Do not carry arbitrary caller headers into a narrow sidecar. MCP's public
    # transport headers are explicit; auth remains the sidecar's responsibility.
    return '''        proxy_http_version 1.1;
        proxy_set_header Host "__IP__";
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_pass_request_headers off;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header Content-Type $http_content_type;
        proxy_set_header Accept $http_accept;
        proxy_set_header Origin $http_origin;
        proxy_set_header MCP-Protocol-Version $http_mcp_protocol_version;
        proxy_set_header MCP-Session-Id $http_mcp_session_id;
        proxy_set_header Last-Event-ID $http_last_event_id;
        proxy_set_header Connection "";
        proxy_connect_timeout 5s;
        proxy_read_timeout 120s;
        proxy_send_timeout 30s;
        proxy_buffering off;
        proxy_request_buffering on;
        proxy_cache off;
        proxy_next_upstream off;
'''.replace('__IP__', ip)


def route(config, path, port, methods, body_limit, metadata=False):
    directives = ['    location = ' + path + ' {',
        '        if ($request_method !~ "^(' + '|'.join(methods) + ')$") { return 405; }',
        '        client_max_body_size ' + str(body_limit) + ';']
    if metadata:
        directives += ['        if ($http_content_length !~ "^(0)?$") { return 413; }']
    else:
        directives += ['        if ($mesh_bearer = 0) { return 401; }',
                       '        if ($mesh_json = 0) { return 415; }']
    if port is None:
        directives += ['        return 503;', '    }']
        return '\n'.join(directives)
    directives += [forwarding(config['public_ip'])]
    if metadata:
        # Public resource metadata is not a general OAuth/management proxy and
        # does not receive bearer/cookie/client forwarding identity.
        directives[-1] = directives[-1].replace('proxy_set_header Authorization $http_authorization;',
                                               'proxy_set_header Authorization "";')
    if path == '/cloud/node':
        directives += ['        if ($http_origin != "") { return 400; }']
    directives += ['        proxy_pass http://127.0.0.1:' + str(port) + path + ';', '    }']
    return '\n'.join(directives)


def nginx(config, tls=False):
    ip = config['public_ip']
    maps = '''    map $http_host $mesh_host80 {
        default 0;
        "__IP__" 1;
        "__IP__:80" 1;
    }
    map $http_host $mesh_host443 {
        default 0;
        "__IP__" 1;
        "__IP__:443" 1;
    }
    map $http_authorization $mesh_bearer {
        default 0;
        "~^Bearer [A-Za-z0-9._~+/-]+=*$" 1;
    }
    map "$request_method:$http_content_type" $mesh_json {
        default 0;
        "POST:application/json" 1;
        "POST:application/json; charset=utf-8" 1;
        "~^(GET|DELETE):" 1;
    }
'''.replace('__IP__', ip)
    http_server = '''    server {
        listen 80 default_server;
        server_name __IP__;
        if ($mesh_host80 = 0) { return 404; }
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
'''.replace('__IP__', ip)
    tls_server = ''
    if tls:
        locations = [route(config, '/mcp', config['mcp_port'], ('POST', 'GET', 'DELETE'), 65536),
            route(config, '/cloud/node', config['cloud_port'], ('POST',), 8 * 1024 * 1024),
            route(config, '/.well-known/oauth-protected-resource/mcp', config['mcp_port'],
                  ('GET',), 1024, metadata=True)]
        tls_server = '''    server {
        listen 443 ssl default_server;
        server_name __IP__;
        ssl_certificate /etc/letsencrypt/live/mesh-ip/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/mesh-ip/privkey.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_tickets off;
        if ($mesh_host443 = 0) { return 404; }
        if ($request_uri != $uri) { return 404; }
        if ($http_transfer_encoding != "") { return 400; }
        if ($http_content_encoding != "") { return 400; }
        if ($http_expect != "") { return 417; }
        if ($http_upgrade != "") { return 400; }
        if ($http_cookie != "") { return 400; }
__LOCATIONS__
        location / { return 404; }
    }
'''.replace('__IP__', ip).replace('__LOCATIONS__', '\n'.join(locations))
    return '''user nginx;
worker_processes 1;
pid /var/run/nginx.pid;
error_log /dev/null crit;
events { worker_connections 128; }
http {
    access_log off;
    server_tokens off;
    client_body_timeout 15s;
    client_header_timeout 15s;
    send_timeout 30s;
    keepalive_timeout 15s;
    client_body_temp_path /var/cache/nginx/client_temp;
    proxy_temp_path /var/cache/nginx/proxy_temp;
__MAPS__
__HTTP__
__TLS__
}
'''.replace('__MAPS__', maps).replace('__HTTP__', http_server).replace('__TLS__', tls_server)


def docker_base(config, name, memory):
    root = config['runtime_root']
    return ['/usr/bin/docker', '--config', root + '/docker-client', 'run', '--name', name,
        '--rm', '--pull=never', '--network=host', '--read-only', '--cap-drop=ALL',
        '--security-opt=no-new-privileges=true', '--pids-limit=64', '--memory=' + memory,
        '--cpus=1', '--log-driver=none', '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m']


def mount(source, target, readonly=False):
    return ['--mount', 'type=bind,src=' + source + ',dst=' + target + (',readonly' if readonly else '')]


def certbot_command(config, mode, renew=False, dry_run=False):
    root = config['runtime_root']
    command = docker_base(config, 'mesh-ip-acme-' + mode, '256m')
    for directory, target in (('letsencrypt-' + mode, '/etc/letsencrypt'),
                              ('work-' + mode, '/var/lib/letsencrypt'),
                              ('logs-' + mode, '/var/log/letsencrypt'), ('webroot', '/mesh-webroot')):
        command += mount(root + '/' + directory, target)
    command += mount(root + '/config', '/mesh-hooks', readonly=True)
    command += [CERTBOT, 'renew' if renew else 'certonly', '--non-interactive',
        '--config-dir', '/etc/letsencrypt', '--work-dir', '/var/lib/letsencrypt',
        '--logs-dir', '/var/log/letsencrypt', '--cert-name', CERT_NAME,
        '--server', STAGING if mode == 'staging' else PRODUCTION,
        '--required-profile', 'shortlived', '--preferred-challenges', 'http',
        '--max-log-backups', '3', '--no-directory-hooks']
    if renew:
        if dry_run:
            # Certbot --dry-run normally selects staging. Override our explicit
            # production server, never test/force against production by accident.
            command[command.index('--server') + 1] = STAGING
            command += ['--dry-run']
    else:
        command += ['--webroot', '--webroot-path', '/mesh-webroot', '--ip-address', config['public_ip'],
            '--agree-tos', '--register-unsafely-without-email', '--no-eff-email', '--keep-until-expiring']
    if mode == 'production':
        command += ['--deploy-hook', '/mesh-hooks/deploy-hook.sh']
    return command


def gateway_command(config, test=False):
    root = config['runtime_root']
    command = docker_base(config, CONTAINER + ('-config-test' if test else ''), '128m')
    # nginx master needs these four capabilities to bind low ports, create its
    # private tmpfs paths and drop worker privileges. No privileged container.
    command += ['--cap-add=NET_BIND_SERVICE', '--cap-add=CHOWN', '--cap-add=SETUID', '--cap-add=SETGID',
        '--tmpfs', '/var/run:rw,noexec,nosuid,size=1m',
        '--tmpfs', '/var/cache/nginx:rw,noexec,nosuid,size=32m']
    command += mount(root + '/config', '/mesh-config', True)
    command += mount(root + '/webroot', '/mesh-webroot', True)
    # Certificate live symlinks need archive, but the public gateway must not
    # receive Certbot's accounts/ ACME private keys or renewal configuration.
    command += mount(root + '/letsencrypt-production/live', '/etc/letsencrypt/live', True)
    command += mount(root + '/letsencrypt-production/archive', '/etc/letsencrypt/archive', True)
    command += ['--entrypoint', 'nginx', NGINX, '-c', '/mesh-config/nginx-current.conf']
    return command + (['-t'] if test else ['-g', 'daemon off;'])


def render(config):
    config = validate(config)
    root = config['runtime_root']
    docker = ['/usr/bin/docker', '--config', root + '/docker-client']
    hook = '''#!/bin/sh
set -eu
umask 077
test "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/mesh-ip
printf '%s\\n' mesh-ip > /var/lib/letsencrypt/mesh-reload-required
'''
    # Persistent marker survives a failed test/reload so the next successful
    # renewal check retries loading the already-issued cert, not issuance.
    reload_commands = '\n'.join(shell(docker + ['exec', CONTAINER] + args)
                                 for args in (['nginx', '-t', '-c', '/mesh-config/nginx-current.conf'],
                                              ['nginx', '-s', 'reload', '-c', '/mesh-config/nginx-current.conf']))
    renew = '''#!/bin/sh
set -eu
umask 077
__RENEW__
if test -f __MARKER__; then
    test "$(/usr/bin/stat -c %u:%a __MARKER__)" = 0:600
    test "$(/usr/bin/head -n 1 __MARKER__)" = mesh-ip
__RELOAD__
    /usr/bin/unlink __MARKER__
fi
'''.replace('__RENEW__', shell(certbot_command(config, 'production', renew=True)))
    renew = renew.replace('__MARKER__', root + '/work-production/mesh-reload-required')
    renew = renew.replace('__RELOAD__', reload_commands)
    service = '''[Unit]
Description=Check dedicated Mesh short-lived IP certificate and load actual renewal
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service
[Service]
Type=oneshot
User=root
ExecStart=__ROOT__/bin/renew.sh
TimeoutStartSec=300
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=null
StandardError=journal
'''.replace('__ROOT__', root)
    timer = '''[Unit]
Description=Twice-daily dedicated Mesh IP certificate check
[Timer]
OnBootSec=15min
OnUnitActiveSec=12h
RandomizedDelaySec=30min
Persistent=true
Unit=personal-assistant-mesh-ip-renew.service
[Install]
WantedBy=timers.target
'''
    gateway = '''[Unit]
Description=Dedicated Mesh IP HTTPS gateway, not the management API
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service
[Service]
Type=simple
User=root
ExecStart=__START__
ExecStop=__STOP__
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
StandardOutput=null
StandardError=journal
[Install]
WantedBy=multi-user.target
'''.replace('__START__', shell(gateway_command(config)))
    gateway = gateway.replace('__STOP__', shell(docker + ['stop', '--time', '20', CONTAINER]))
    directories = [root] + [root + '/' + p for p in ('config', 'bin', 'docker-client',
        'letsencrypt-production', 'letsencrypt-production/live', 'letsencrypt-production/archive',
        'letsencrypt-staging', 'work-production', 'work-staging',
        'logs-production', 'logs-staging')]
    plan = {'format': 'mesh-ip-tls-owner-plan/1', 'renders_only': True,
        'requires_serial_operations_owner': True, 'public_ip': config['public_ip'],
        'sidecars_configured': {'mcp': config['mcp_port'] is not None, 'cloud': config['cloud_port'] is not None},
        'commands': {'create_private_directories': ['/usr/bin/install', '-d', '-m', '0700'] + directories,
            'create_public_challenge_root': ['/usr/bin/install', '-d', '-m', '0755', root + '/webroot'],
            'pull_certbot_explicit_digest': docker + ['pull', CERTBOT],
            'pull_nginx_explicit_digest': docker + ['pull', NGINX],
            'test_gateway_current_config': gateway_command(config, test=True),
            'run_gateway_foreground': gateway_command(config),
            'issue_staging': certbot_command(config, 'staging'),
            'issue_production': certbot_command(config, 'production'),
            'dry_run_production_renewal': certbot_command(config, 'production', renew=True, dry_run=True)},
        'management_api_publicly_routed': False, 'host_proxy_modified': False,
        'issuance_or_account_created': False, 'renewal_interval_hours': 12}
    return {'nginx-http.conf': nginx(config), 'nginx-tls.conf': nginx(config, tls=True),
            'nginx-current.conf': nginx(config), 'deploy-hook.sh': hook, 'renew.sh': renew,
            'personal-assistant-mesh-ip-tls.service': gateway,
            'personal-assistant-mesh-ip-renew.service': service,
            'personal-assistant-mesh-ip-renew.timer': timer,
            'plan.json': json.dumps(plan, indent=2, sort_keys=True) + '\n'}


def build(config, destination):
    files = render(config)
    destination = Path(destination)
    if (not destination.is_absolute() or '..' in destination.parts or destination.exists()
            or any(p.is_symlink() for p in destination.parents)):
        raise ValueError('ip_tls_new_absolute_destination_required')
    destination.mkdir(mode=0o700)
    manifest = {}
    for name, text in files.items():
        path = destination / name
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o700 if name.endswith('.sh') else 0o600)
        raw = text.encode('utf8')
        with os.fdopen(descriptor, 'wb') as target:
            target.write(raw)
        manifest[name] = hashlib.sha256(raw).hexdigest()
    return {'rendered_only': True, 'files': manifest, 'network_or_deploy_performed': False,
            'account_or_key_created': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-config', required=True)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args(argv)
    from assistant_mesh.config import private_json
    try:
        value = build(private_json(args.owner_config), args.destination)
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'rendered_only': True, 'error': 'ip_tls_render_rejected'}))
        return 2
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
