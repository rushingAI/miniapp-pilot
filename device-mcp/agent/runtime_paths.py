"""Runtime storage is independent of the checkout and explicit overrides only."""
import os
from pathlib import Path

def data_root() -> Path:
    configured = os.environ.get('MINIAPP_PILOT_DATA_DIR', '').strip()
    return Path(configured).expanduser().resolve() if configured else Path.home() / '.miniapp-pilot'

