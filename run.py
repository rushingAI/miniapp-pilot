"""Start the loopback-only development application."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'device-mcp'))

def main():
    parser = argparse.ArgumentParser(description='MiniApp Pilot local source runtime')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--check', action='store_true', help='Import the complete runtime without starting a device or model session')
    args = parser.parse_args()
    from agent.web.app import app
    if args.check:
        from agent.web.harness_session import HarnessSession
        print('Runtime imports OK')
        return
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port)

if __name__ == '__main__':
    main()
