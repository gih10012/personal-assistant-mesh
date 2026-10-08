"""Save only proxy variables for a background worker; no values on stdout."""
import argparse
import os

from scripts.configure import save
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--destination', required=True)
args = parser.parse_args()
os.umask(0o077)
values = {k: os.environ[k] for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                                    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy') if k in os.environ}
save(Path(args.destination), values)
print('Private proxy configuration saved; no credential values printed.')
