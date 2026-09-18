"""Create a local source-review archive from the explicit public inventory."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile
from release_check import ROOT, check, inspect_bytes

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    names, issues = check()
    if issues:
        raise SystemExit(json.dumps(issues, ensure_ascii=False))
    sys.path.insert(0, str(ROOT/'device-mcp/agent'))
    from build_version import APP_VERSION
    destination = args.output.resolve()
    if destination.exists():
        raise SystemExit('Refusing to overwrite an existing archive')
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = f'miniapp-pilot-{APP_VERSION}'
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            info = zipfile.ZipInfo(prefix+'/'+name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (ROOT/name).read_bytes())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise SystemExit('Archive readback failed')
        assert sorted(archive.namelist()) == sorted(prefix+'/'+name for name in names)
        for name in names:
            raw = archive.read(prefix+'/'+name)
            assert raw == (ROOT/name).read_bytes()
            assert not inspect_bytes(name, raw)
    print(json.dumps({'archive': str(destination), 'files': len(names),
                      'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
                      'publication': 'not performed'}))

if __name__ == '__main__':
    main()
