"""Download component's owner-pinned skill reused for authority-local uploads.

No account is mounted/copied into the SDK runtime: only the already-authorized
authority's three required fields enter stdin. No sendmessage/getupdates occurs.
"""
import base64
import hashlib
import json
from pathlib import Path
import subprocess

from .config import private_json
from .entrance_media import SkillMediaDownloader


_UPLOAD_CHILD = r'''
import base64,hashlib,importlib.util,json,os,pathlib,stat,sys
try:
 folder,media_sha,ilink_sha=sys.argv[1:]
 modules=[]
 for name,expected in [('ilink',ilink_sha),('ilink_media',media_sha)]:
  p=pathlib.Path(folder)/(name+'.py');s=p.lstat();raw=p.read_bytes()
  if not stat.S_ISREG(s.st_mode) or s.st_uid not in (0,os.getuid()) or s.st_mode&0o022 or hashlib.sha256(raw).hexdigest()!=expected:raise ValueError()
  spec=importlib.util.spec_from_file_location(name,str(p));m=importlib.util.module_from_spec(spec);sys.modules[name]=m
  exec(compile(raw,str(p),'exec'),m.__dict__);modules.append(m)
 import importlib.metadata
 if importlib.metadata.version('wechatbot-sdk')!='0.3.0':raise ValueError()
 value=json.loads(sys.stdin.buffer.read(150*1024*1024))
 data=base64.b64decode(value['data'],validate=True)
 result=modules[1].upload(value['state'],data,value['kind'],value['filename'])
 sys.stdout.write(json.dumps(result,separators=(',',':')))
except Exception:
 sys.exit(22)
'''


class SkillMediaUploader:
    """Trusted owner installation only, never model-supplied account/config."""
    def __init__(self, account_file, media_helper_file, helper_sha256, ilink_sha256, python=None):
        self.runtime = SkillMediaDownloader(media_helper_file, helper_sha256, ilink_sha256, python)
        self.account_file = str(Path(account_file))
        account = private_json(self.account_file)
        self.binding = self._binding(account)

    @staticmethod
    def _binding(account):
        if not isinstance(account, dict) or any(not isinstance(account.get(key), str) or not account[key]
                for key in ('baseurl', 'ilink_user_id', 'ilink_bot_id', 'bot_token')):
            raise ValueError('claw_authority_account_required')
        return hashlib.sha256(json.dumps([account.get(key) for key in
            ('baseurl', 'ilink_user_id', 'ilink_bot_id')], separators=(',', ':')).encode()).hexdigest()

    def __call__(self, data, kind, filename):
        account = private_json(self.account_file)
        if self._binding(account) != self.binding:
            raise ValueError('claw_upload_owner_binding_changed')
        state = {key: account[key] for key in ('baseurl', 'bot_token', 'ilink_user_id')}
        payload = {'state': state, 'data': base64.b64encode(data).decode('ascii'),
                   'kind': kind, 'filename': filename}
        try:
            result = subprocess.run([self.runtime.python, '-I', '-c', _UPLOAD_CHILD,
                str(self.runtime.path.parent), self.runtime.helper_sha256, self.runtime.ilink_sha256],
                input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=55, check=False)
            if result.returncode or len(result.stdout) > 65536:
                raise ValueError()
            item = json.loads(result.stdout.decode('utf8'))
            expected = {'image': 2, 'file': 4, 'video': 5}[kind]
            if not isinstance(item, dict) or item.get('type') != expected:
                raise ValueError()
            return item
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            raise ValueError('claw_upload_outcome_unconfirmed') from None


def uploader_from_config(config):
    options = config.get('entrance_media')
    if not isinstance(options, dict) or not {'media_helper_file', 'media_helper_sha256', 'ilink_sha256'} <= set(options):
        return None
    return SkillMediaUploader(config['ilink_account'], options['media_helper_file'],
        options['media_helper_sha256'], options['ilink_sha256'], options.get('python'))
