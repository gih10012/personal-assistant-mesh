"""Small pure renderer/argv/source checks; no Docker, CA or account operation."""
import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location('prepare_hostname_tls',
    str(Path(__file__).resolve().parents[1] / 'deploy' / 'prepare-hostname-tls.py'))
tls = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tls)


class HostnameTLSPreparationTests(unittest.TestCase):
    def setUp(self):
        self.original = {'public_ip': '8.8.8.8', 'runtime_root': '/opt/mesh-tls-fixture',
                         'mcp_port': 18685, 'oauth_port': 18685, 'cloud_port': 18686}
        self.profile = {'hostname': '8-8-8-8.sslip.io', 'mcp_port': 3290,
                        'oauth_port': 3290, 'cloud_port': 3291,
                        'accounts': {'staging': 'a' * 32, 'production': 'b' * 32}}
        self.config = dict(self.original, dns_profile=self.profile)

    def test_unconfigured_assets_and_ip_renewal_remain_byte_identical(self):
        for value in (self.original, dict(self.original, dns_profile=None)):
            self.assertEqual(tls.ip_tls.render(self.original), tls.render(value))
        original, combined = tls.ip_tls.render(self.original), tls.render(self.config)
        for name in ('deploy-hook.sh', 'renew.sh', 'personal-assistant-mesh-ip-renew.service',
                     'personal-assistant-mesh-ip-renew.timer'):
            self.assertEqual(original[name], combined[name], name)
        for name in ('nginx-http.conf', 'nginx-tls.conf'):
            # Only global DNS maps and additional server blocks are inserted.
            old_servers = original[name][original[name].index('    server {'):].rsplit('}\n', 1)[0]
            self.assertIn(old_servers, combined[name])

    def test_dual_vhosts_keep_exact_hosts_routes_headers_and_require_new_mounts(self):
        assets = tls.render(self.config)
        active = assets['nginx-tls.conf']
        self.assertEqual(2, active.count('location = /mcp {'))
        self.assertEqual(2, active.count('location = /cloud/node {'))
        self.assertEqual(2, active.count('location = /authorize {'))
        self.assertNotIn('/v1/', active)
        self.assertIn('proxy_set_header Host "8.8.8.8";', active)
        self.assertIn('proxy_set_header Host "8-8-8-8.sslip.io";', active)
        self.assertIn('proxy_pass http://127.0.0.1:3290/token;', active)
        self.assertIn('proxy_pass http://127.0.0.1:3291/cloud/node;', active)
        self.assertIn('if ($mesh_dns_host443 = 0) { return 404; }', active)
        self.assertIn('proxy_pass_request_headers off;', active)
        self.assertIn('proxy_next_upstream off;', active)
        self.assertIn('if ($mesh_tls_cookie = 0) { return 400; }', active)
        self.assertEqual(2, active.count('if ($http_transfer_encoding != "") { return 400; }'))
        challenges = [line for line in active.splitlines() if 'location ~' in line]
        self.assertEqual(2, len(challenges))
        self.assertEqual(challenges[0], challenges[1])
        bootstrap = assets['nginx-dns-bootstrap.conf']
        self.assertEqual(1, bootstrap.count('listen 443 ssl'))
        self.assertIn('server_name 8-8-8-8.sslip.io;', bootstrap)
        self.assertNotIn('/etc/letsencrypt-dns/', bootstrap)
        self.assertEqual(bootstrap, assets['nginx-current.conf'])
        plan = json.loads(assets['plan.json'])
        self.assertTrue(plan['dns_profile']['required_tls_container_recreate'])
        self.assertIn('dst=/etc/letsencrypt-dns/live,readonly', ' '.join(plan['commands']['run_gateway_foreground']))
        self.assertIn('dst=/etc/letsencrypt-dns/archive,readonly', assets['personal-assistant-mesh-ip-tls.service'])
        # DNS OAuth still has its maps when the IP deployment has no AS routes.
        without_ip_as = dict(self.config, oauth_port=None)
        text = tls.render(without_ip_as)['nginx-tls.conf']
        self.assertEqual(1, text.count('map $request_uri $mesh_tls_target {'))

    def test_dns_issuance_renewal_and_hooks_are_separate_and_never_replace_active_config(self):
        assets = tls.render(self.config)
        plan = json.loads(assets['plan.json'])
        for mode in ('staging', 'production'):
            command = plan['commands']['dns_issue_' + mode]
            self.assertEqual('mesh-dns', command[command.index('--cert-name') + 1])
            self.assertEqual(self.profile['hostname'], command[command.index('-d') + 1])
            self.assertIn('/letsencrypt-dns-' + mode + ',dst=/etc/letsencrypt', ' '.join(command))
            self.assertIn('/letsencrypt-' + mode + '/accounts,dst=/etc/letsencrypt/accounts,readonly', ' '.join(command))
            self.assertEqual(self.profile['accounts'][mode], command[command.index('--account') + 1])
            self.assertNotIn('--ip-address', command)
            self.assertNotIn('--required-profile', command)
            self.assertNotIn('--register-unsafely-without-email', command)
            self.assertNotIn('--agree-tos', command)
            self.assertNotIn('--force-renewal', command)
        dry = plan['commands']['dns_dry_run_production_renewal']
        self.assertIn(tls.ip_tls.STAGING, dry)
        self.assertNotIn(tls.ip_tls.PRODUCTION, dry)
        self.assertIn('--dry-run', dry)
        self.assertIn('--no-random-sleep-on-renew', dry)
        self.assertEqual(self.profile['accounts']['staging'], dry[dry.index('--account') + 1])
        self.assertIn('/letsencrypt-staging/accounts,dst=/etc/letsencrypt/accounts,readonly', ' '.join(dry))
        renew = assets['renew-dns.sh']
        self.assertIn('/mesh-config/nginx-current.conf', renew)
        self.assertNotIn('nginx-tls.conf', renew)
        self.assertNotIn('nginx-dns-bootstrap.conf', renew)
        self.assertNotIn(' cp ', renew)
        self.assertNotIn('nginx-current.conf', assets['deploy-hook-dns.sh'])
        with tempfile.TemporaryDirectory(prefix='mesh-dns-render.') as folder:
            for name in ('deploy-hook-dns.sh', 'renew-dns.sh'):
                path = Path(folder) / name
                path.write_text(assets[name])
                self.assertEqual(0, subprocess.run(['/bin/sh', '-n', str(path)], check=False).returncode)

    def test_profile_validation_new_only_writer_and_legacy_grammar(self):
        for hostname in ('8.8.8.8', 'localhost', '*.sslip.io', 'HOST.sslip.io',
                         'host.sslip.io.', 'https://host.sslip.io', '-host.sslip.io', 'host..sslip.io',
                         'host.sslip.io:443', 'host.sslip.io\n', 'a' * 64 + '.sslip.io'):
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                tls.render(dict(self.original, dns_profile=dict(self.profile, hostname=hostname)))
        for changes in ({'mcp_port': 18685, 'oauth_port': 18685},
                        {'oauth_port': 3292}, {'cloud_port': 3290}, {'mcp_port': True},
                        {'host_header': 'other'}, {'accounts': {}},
                        {'accounts': {'staging': 'not-an-account', 'production': 'b' * 32}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                tls.render(dict(self.original, dns_profile=dict(self.profile, **changes)))
        with tempfile.TemporaryDirectory(prefix='mesh-dns-build.') as folder:
            target = Path(folder) / 'new'
            result = tls.build(self.config, target)
            self.assertFalse(result['network_or_deploy_performed'])
            self.assertEqual(0o700, target.stat().st_mode & 0o777)
            self.assertEqual(0o600, (target / 'plan.json').stat().st_mode & 0o777)
            with self.assertRaises(ValueError):
                tls.build(self.config, target)
        source = Path(tls.__file__).read_text()
        ast.parse(source, feature_version=(3, 6)) if sys.version_info >= (3, 8) else ast.parse(source)


if __name__ == '__main__':
    unittest.main()
