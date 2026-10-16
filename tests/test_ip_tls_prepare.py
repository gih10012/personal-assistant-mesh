"""Pure asset/argv tests; no certificate, CA account, Docker or listener created."""
import importlib.util
import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location('prepare_ip_tls',
    str(Path(__file__).resolve().parents[1] / 'deploy' / 'prepare-ip-tls.py'))
tls = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tls)


class IPTLSPreparationTests(unittest.TestCase):
    def setUp(self):
        self.config = {'public_ip': '8.8.8.8', 'runtime_root': '/opt/mesh-ip-tls-test',
                       'mcp_port': None, 'cloud_port': None}

    def test_bootstrap_is_challenge_only_and_no_tls_private_key_is_read(self):
        assets = tls.render(self.config)
        bootstrap = assets['nginx-current.conf']
        self.assertEqual(bootstrap, assets['nginx-http.conf'])
        self.assertIn('listen 80 default_server', bootstrap)
        self.assertNotIn('listen 443', bootstrap)
        self.assertNotIn('ssl_certificate', bootstrap)
        self.assertNotIn('proxy_pass http', bootstrap)
        self.assertIn('if ($request_method != GET) { return 405; }', bootstrap)
        self.assertIn('location / { return 404; }', bootstrap)
        self.assertIn('try_files $uri =404', bootstrap)
        self.assertIn('if ($request_uri != $uri) { return 404; }', bootstrap)
        self.assertNotIn('return 301', bootstrap)

    def test_https_defaults_fail_closed_and_has_no_management_route(self):
        config = tls.render(self.config)['nginx-tls.conf']
        self.assertEqual(3, config.count('return 503;'))
        self.assertEqual(2, config.count('if ($mesh_bearer = 0) { return 401; }'))
        self.assertNotIn('proxy_pass http', config)
        self.assertEqual(1, config.count('location = /mcp {'))
        self.assertEqual(1, config.count('location = /cloud/node {'))
        self.assertEqual(1, config.count('location = /.well-known/oauth-protected-resource/mcp {'))
        self.assertNotIn('/v1/', config)
        self.assertNotIn('17680', config)
        self.assertIn('ssl_protocols TLSv1.2 TLSv1.3;', config)
        self.assertIn('client_max_body_size 65536;', config)
        self.assertIn('client_max_body_size 8388608;', config)
        self.assertIn('if ($mesh_host443 = 0) { return 404; }', config)

    def test_configured_sidecars_forward_only_exact_uri_and_rebuild_trust_headers(self):
        self.config.update(mcp_port=18685, cloud_port=18686)
        config = tls.render(self.config)['nginx-tls.conf']
        self.assertIn('proxy_pass http://127.0.0.1:18685/mcp;', config)
        self.assertIn('proxy_pass http://127.0.0.1:18686/cloud/node;', config)
        self.assertIn('proxy_pass http://127.0.0.1:18685/.well-known/oauth-protected-resource/mcp;', config)
        self.assertIn('proxy_pass_request_headers off;', config)
        self.assertIn('proxy_set_header Host "8.8.8.8";', config)
        self.assertIn('proxy_set_header X-Forwarded-Proto https;', config)
        self.assertIn('proxy_set_header X-Forwarded-For $remote_addr;', config)
        self.assertIn('proxy_set_header Authorization "";', config)
        self.assertNotIn('$proxy_add_x_forwarded_for', config)
        self.assertNotIn('$http_x_forwarded_proto', config)
        self.assertIn('proxy_next_upstream off;', config)
        self.assertIn('if ($http_transfer_encoding != "") { return 400; }', config)
        self.assertIn('if ($http_content_encoding != "") { return 400; }', config)
        self.assertIn('if ($http_cookie != "") { return 400; }', config)
        self.assertIn('access_log off;', config)
        self.assertIn('error_log /dev/null crit;', config)

    def test_configured_mcp_preserves_sdk_oauth_challenge_cloud_keeps_token_gate(self):
        mcp = tls.route(self.config, '/mcp', 18685, ('POST', 'GET', 'DELETE'), 65536)
        self.assertNotIn('$mesh_bearer', mcp)
        self.assertIn('proxy_pass http://127.0.0.1:18685/mcp;', mcp)
        self.assertIn('proxy_set_header Authorization $http_authorization;', mcp)
        self.assertIn('proxy_intercept_errors off;', mcp)
        self.assertNotIn('proxy_hide_header', mcp)
        self.assertIn('if ($mesh_json = 0) { return 415; }', mcp)
        cloud = tls.route(self.config, '/cloud/node', 18686, ('POST',), 8388608)
        self.assertIn('if ($mesh_bearer = 0) { return 401; }', cloud)
        self.assertIn('if ($http_origin != "") { return 400; }', cloud)
        self.assertIn('proxy_next_upstream off;', cloud)

    def test_missing_mcp_sidecar_never_falls_back_to_unauthenticated_proxy(self):
        mcp = tls.route(self.config, '/mcp', None, ('POST', 'GET', 'DELETE'), 65536)
        self.assertIn('if ($mesh_bearer = 0) { return 401; }', mcp)
        self.assertIn('return 503;', mcp)
        self.assertNotIn('proxy_pass', mcp)
        self.assertNotIn('resource_metadata=', mcp)  # No imaginary ready OAuth server.

    def test_issuance_uses_exact_official_digest_shortlived_webroot_and_no_email(self):
        plan = json.loads(tls.render(self.config)['plan.json'])
        command = plan['commands']['issue_production']
        self.assertIn(tls.CERTBOT, command)
        self.assertIn('--pull=never', command)
        self.assertIn('--network=host', command)
        self.assertIn('--cap-drop=ALL', command)
        self.assertIn('--read-only', command)
        self.assertIn('--register-unsafely-without-email', command)
        self.assertNotIn('--email', command)
        self.assertEqual('shortlived', command[command.index('--required-profile') + 1])
        self.assertEqual('http', command[command.index('--preferred-challenges') + 1])
        self.assertEqual(tls.PRODUCTION, command[command.index('--server') + 1])
        self.assertEqual('8.8.8.8', command[command.index('--ip-address') + 1])
        self.assertIn('--webroot', command)
        self.assertNotIn('--nginx', command)
        self.assertNotIn('--apache', command)
        self.assertNotIn('--force-renewal', command)
        self.assertNotIn('--privileged', command)
        self.assertFalse(any('docker.sock' in arg for arg in command))
        self.assertIn('/mesh-hooks/deploy-hook.sh', command)

    def test_staging_state_is_separate_and_renewal_dry_run_never_uses_production_ca(self):
        staging = tls.certbot_command(self.config, 'staging')
        production = tls.certbot_command(self.config, 'production')
        self.assertIn(tls.STAGING, staging)
        self.assertNotIn(tls.PRODUCTION, staging)
        self.assertTrue(any('/letsencrypt-staging,' in arg for arg in staging))
        self.assertTrue(any('/letsencrypt-production,' in arg for arg in production))
        self.assertNotIn('--deploy-hook', staging)
        dry = tls.certbot_command(self.config, 'production', renew=True, dry_run=True)
        self.assertIn('--dry-run', dry)
        self.assertIn(tls.STAGING, dry)
        self.assertNotIn(tls.PRODUCTION, dry)
        self.assertNotIn('--run-deploy-hooks', dry)

    def test_every_renew_disables_only_internal_sleep_retaining_scheduler_jitter(self):
        for mode in ('staging', 'production'):
            for dry_run in (False, True):
                with self.subTest(mode=mode, dry_run=dry_run):
                    command = tls.certbot_command(self.config, mode, renew=True, dry_run=dry_run)
                    self.assertEqual(1, command.count('--no-random-sleep-on-renew'))
                    self.assertEqual('renew', command[command.index(tls.CERTBOT) + 1])
                    self.assertNotIn('--force-renewal', command)
                    self.assertNotIn('--run-deploy-hooks', command)
            self.assertNotIn('--no-random-sleep-on-renew', tls.certbot_command(self.config, mode))
        assets = tls.render(self.config)
        self.assertIn('--no-random-sleep-on-renew', assets['renew.sh'])
        dry = json.loads(assets['plan.json'])['commands']['dry_run_production_renewal']
        self.assertEqual(1, dry.count('--no-random-sleep-on-renew'))
        self.assertEqual(tls.STAGING, dry[dry.index('--server') + 1])
        self.assertIn('TimeoutStartSec=300', assets['personal-assistant-mesh-ip-renew.service'])
        self.assertIn('OnUnitActiveSec=12h', assets['personal-assistant-mesh-ip-renew.timer'])
        self.assertIn('RandomizedDelaySec=30min', assets['personal-assistant-mesh-ip-renew.timer'])

    def test_gateway_hardening_and_renew_hook_never_mounts_docker_socket_in_container(self):
        assets = tls.render(self.config)
        command = json.loads(assets['plan.json'])['commands']['run_gateway_foreground']
        self.assertEqual(tls.NGINX, command[command.index('--entrypoint') + 2])
        for argument in ('--cap-drop=ALL', '--read-only', '--network=host', '--pull=never',
                         '--security-opt=no-new-privileges=true'):
            self.assertIn(argument, command)
        self.assertEqual({'--cap-add=CHOWN', '--cap-add=SETUID', '--cap-add=SETGID',
                          '--cap-add=NET_BIND_SERVICE'}, {a for a in command if a.startswith('--cap-add=')})
        self.assertFalse(any('docker.sock' in arg or 'src=/home' in arg or 'src=/root' in arg for arg in command))
        self.assertTrue(all('readonly' in command[i + 1] for i, arg in enumerate(command) if arg == '--mount'))
        mounts = [command[i + 1] for i, arg in enumerate(command) if arg == '--mount']
        self.assertTrue(any('/letsencrypt-production/live,' in value for value in mounts))
        self.assertTrue(any('/letsencrypt-production/archive,' in value for value in mounts))
        self.assertFalse(any('dst=/etc/letsencrypt,' in value for value in mounts))
        self.assertNotIn('--publish', command)
        self.assertIn('OnUnitActiveSec=12h', assets['personal-assistant-mesh-ip-renew.timer'])
        self.assertIn('RandomizedDelaySec=30min', assets['personal-assistant-mesh-ip-renew.timer'])
        self.assertIn('RENEWED_LINEAGE', assets['deploy-hook.sh'])
        self.assertNotIn('docker', assets['deploy-hook.sh'])
        self.assertIn('mesh-reload-required', assets['renew.sh'])
        self.assertLess(assets['renew.sh'].index('nginx -t'), assets['renew.sh'].index('nginx -s reload'))
        self.assertLess(assets['renew.sh'].index('nginx -s reload'), assets['renew.sh'].index('/usr/bin/unlink'))
        for name in ('deploy-hook.sh', 'renew.sh'):
            result = subprocess.run(['/bin/sh', '-n'], input=assets[name].encode(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(0, result.returncode, (name, result.stderr))

    def test_only_new_destination_written_and_no_external_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / 'new'
            result = tls.build(self.config, destination)
            self.assertTrue(result['rendered_only'])
            self.assertFalse(result['network_or_deploy_performed'])
            self.assertFalse(result['account_or_key_created'])
            self.assertEqual(0o700, destination.stat().st_mode & 0o777)
            self.assertEqual(0o700, (destination / 'renew.sh').stat().st_mode & 0o777)
            self.assertEqual(0o600, (destination / 'nginx-current.conf').stat().st_mode & 0o777)
            before = (destination / 'nginx-current.conf').read_bytes()
            with self.assertRaises(ValueError):
                tls.build(self.config, destination)
            self.assertEqual(before, (destination / 'nginx-current.conf').read_bytes())

    def test_config_injection_private_address_management_ports_and_unknown_fields_rejected(self):
        cases = [('public_ip', '127.0.0.1'), ('public_ip', '8.8.8.8; touch /tmp/no'),
                 ('public_ip', '::1'), ('runtime_root', '/'), ('runtime_root', '/opt/../etc'),
                 ('runtime_root', '/opt/tls;nginx'), ('mcp_port', True), ('mcp_port', 17680),
                 ('cloud_port', 2053), ('cloud_port', '18686')]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                config = dict(self.config)
                config[key] = value
                tls.render(config)
        for config in (dict(self.config, owner_token='forbidden'),
                       dict(self.config, mcp_port=18685, cloud_port=18685)):
            with self.assertRaises(ValueError):
                tls.render(config)

    def oauth_config(self):
        return dict(self.config, mcp_port=18685, cloud_port=18686, oauth_port=18685)

    def test_oauth_absent_or_null_keeps_every_original_asset_byte_identical(self):
        cases = [(None, None, '3b9932982348f457f111dd8a5a49e006e4a010325998b87a2e2ce7614801ed8a'),
                 (18685, 18686, '2b3ffd1990daa7f3295c5675c6d437be19495233761ad14b8ba55ce4a0d23d45')]
        for mcp, cloud, expected in cases:
            config = dict(self.config, mcp_port=mcp, cloud_port=cloud)
            original = tls.render(config)
            raw = json.dumps(original, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
            self.assertEqual(expected, hashlib.sha256(raw).hexdigest())
            self.assertEqual(original, tls.render(dict(config, oauth_port=None)))

    def test_oauth_requires_the_single_existing_mcp_sidecar_not_another_process_or_management_port(self):
        for config in (dict(self.config, oauth_port=18685),
                       dict(self.oauth_config(), oauth_port=18687),
                       dict(self.oauth_config(), oauth_port=18686),
                       dict(self.oauth_config(), oauth_port=17680),
                       dict(self.oauth_config(), oauth_port=True),
                       dict(self.oauth_config(), oauth_port='18685')):
            with self.subTest(config=config), self.assertRaises(ValueError):
                tls.render(config)

    def test_oauth_adds_only_exact_author_routes_to_the_same_backend_and_no_http_login(self):
        assets = tls.render(self.oauth_config())
        config = assets['nginx-tls.conf']
        for path in tls.OAUTH_PATHS:
            self.assertEqual(1, config.count('location = ' + path + ' {'))
            self.assertIn('proxy_pass http://127.0.0.1:18685' + path + ';', config)
        self.assertEqual(9, config.count('location = '))
        self.assertNotIn('location /oauth', config)
        self.assertNotIn('location = /revoke', config)
        self.assertNotIn('/v1/', config)
        self.assertIn('location / { return 404; }', config)
        self.assertIn('if ($mesh_tls_target = 0) { return 404; }', config)
        self.assertIn('if ($mesh_tls_cookie = 0) { return 400; }', config)
        self.assertNotIn('proxy_pass http', assets['nginx-http.conf'])
        self.assertNotIn('location = /authorize', assets['nginx-http.conf'])
        self.assertEqual(assets['nginx-http.conf'], assets['nginx-current.conf'])
        plan = json.loads(assets['plan.json'])
        self.assertTrue(plan['oauth_authorization_server']['same_owned_mcp_sidecar'])
        self.assertFalse(plan['management_api_publicly_routed'])

    def test_oauth_raw_queries_preserve_encoded_values_order_and_reject_unknown_duplicate_or_ambiguous_keys(self):
        for path, keys in tls.OAUTH_QUERIES.items():
            pattern = tls.oauth_query_pattern(path, keys)
            forward = path + '?' + '&'.join(key + '=https%3A%2F%2Fexample.invalid%2Fcb%3Fx%3D1' for key in keys)
            reverse = path + '?' + '&'.join(key + '=scope+with+spaces' for key in reversed(keys))
            self.assertIsNotNone(re.fullmatch(pattern, forward))
            self.assertIsNotNone(re.fullmatch(pattern, reverse))
            self.assertIsNone(re.fullmatch(pattern, forward + '&' + keys[0] + '=again'))
            for key in keys:
                self.assertIsNone(re.fullmatch(pattern, path + '?' + key + '=a&' + key + '=b'))
            for query in ('arbitrary=value', keys[0] + '=bad%XX', keys[0] + '=bad%2',
                          keys[0] + '=raw/uri', keys[0] + '=one&&' + keys[-1] + '=two',
                          keys[0] + '[]=' + 'x', keys[0] + '=' + 'a' * 8193):
                self.assertIsNone(re.fullmatch(pattern, path + '?' + query), query)
            for prefix in ('//', '/%61', '/x/../'):
                self.assertIsNone(re.fullmatch(pattern, prefix + path.lstrip('/') + '?' + keys[0] + '=x'))

    def test_oauth_front_channel_accepts_only_actual_quoted_base64_cookie_names(self):
        pattern = tls.oauth_cookie_pattern()
        state = '__Host-MCP_CONSENT_STATE_' + 'a' * 32
        value = '"abc/def+=.ghi/jkl="'
        for cookies in ('__Host-MCP_CONSENT_BINDING=' + value, state + '=' + value,
                        '__Host-MCP_CONSENT_BINDING=' + value + '; ' + state + '=' + value,
                        state + '=""'):
            self.assertIsNotNone(re.fullmatch(pattern, '1:' + cookies))
            self.assertIsNone(re.fullmatch(pattern, '0:' + cookies))
        for cookies in ('other=private', '__Host-MCP_CONSENT_STATE=' + value,
                        '__Host-MCP_APPROVED_CLIENTS=' + value, '__Host-MCP_DENIED_CLIENTS=' + value,
                        '__Host-MCP_CONSENT_STATE_' + 'A' * 32 + '=' + value,
                        state + '=' + value + '; other=secret', state + '=' + 'a' * 16385):
            self.assertIsNone(re.fullmatch(pattern, '1:' + cookies))

    def test_oauth_method_form_body_and_cookie_gates_keep_upstream_authority_and_no_replay(self):
        config = tls.render(self.oauth_config())['nginx-tls.conf']
        for path, methods, options in (
                ('/authorize', ('GET', 'HEAD', 'POST'), {'form': True}),
                ('/auth/callback', ('GET', 'HEAD'), {}),
                ('/consent', ('GET', 'HEAD', 'POST'), {'form': True}),
                ('/token', ('POST', 'OPTIONS'), {'form': True}),
                ('/register', ('POST', 'OPTIONS'), {'registration': True}),
                (tls.OAUTH_PATHS[0], ('GET', 'HEAD', 'OPTIONS'), {})):
            part = tls.oauth_route(self.oauth_config(), path, methods, **options)
            self.assertIn('^(' + '|'.join(methods) + ')$', part)
            self.assertIn('if ($mesh_oauth_post_query = 1) { return 404; }', part)
            self.assertIn('if ($mesh_oauth_body = 0) { return 413; }', part)
            self.assertIn('proxy_set_header Authorization "";', part)
            self.assertNotIn('$http_authorization', part)
            self.assertIn('proxy_set_header Origin $http_origin;', part)
            self.assertIn('proxy_set_header Referer $http_referer;', part)
            self.assertIn('proxy_set_header Sec-Fetch-Site $http_sec_fetch_site;', part)
            self.assertIn('proxy_set_header Access-Control-Request-Method $http_access_control_request_method;', part)
            self.assertIn('proxy_redirect off;', part)
            self.assertIn('proxy_next_upstream off;', part)
            self.assertIn('proxy_cache off;', part)
            self.assertIn('add_header Cache-Control "no-store" always;', part)
            self.assertNotIn('Access-Control-Allow-Origin', part)
            self.assertNotIn('proxy_hide_header Location', part)
            if path in tls.OAUTH_FRONT_CHANNEL:
                self.assertIn('proxy_set_header Cookie $http_cookie;', part)
                self.assertNotIn('proxy_hide_header Set-Cookie;', part)
            else:
                self.assertIn('proxy_hide_header Set-Cookie;', part)
        self.assertIn('proxy_pass http://127.0.0.1:18686/cloud/node;', config)
        self.assertIn('if ($http_origin != "") { return 400; }', config)
        self.assertNotIn('proxy_pass http://127.0.0.1:17680', config)
