import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
for folder in (ROOT / 'device-mcp', ROOT / 'device-mcp/agent', ROOT / 'device-mcp/agent/web'):
    sys.path.insert(0, str(folder))
# Tests must never open a user's saved credentials or runtime files.
os.environ['MINIAPP_PILOT_DATA_DIR'] = tempfile.mkdtemp(prefix='miniapp-pilot-tests-')
