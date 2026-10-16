"""Import the official 26.3 server's generated/reports/blocks.json.

Generate using Java 25: java -DbundlerMainClass=net.minecraft.data.Main
-jar server.jar --reports. The server URL and SHA-1 come from Mojang's manifest.
"""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('version_metadata', type=Path)
    parser.add_argument('server_jar', type=Path)
    args = parser.parse_args()
    metadata = json.loads(args.version_metadata.read_text())
    if metadata['id'] != '26.3':
        raise SystemExit(f"Only the exact 26.3 release is supported, got {metadata['id']!r}.")
    server = metadata['downloads']['server']
    if hashlib.sha1(args.server_jar.read_bytes()).hexdigest() != server['sha1']:
        raise SystemExit('Server SHA-1 mismatch: the server jar does not match the version metadata.')
    report = json.loads(args.report.read_text())
    properties = {name: block.get('properties', {}) for name, block in sorted(report.items())}
    defaults = {name: next(state.get('properties', {}) for state in block['states'] if state.get('default'))
                for name, block in sorted(report.items())}
    # Vanilla state definitions enumerate the Cartesian product of property domains.
    import math
    for name, block in report.items():
        if len(block['states']) != math.prod(len(v) for v in properties[name].values()):
            raise SystemExit(f'Block report is inconsistent: {name} does not list every property combination.')
    destination = ROOT / 'src/worldprompt_core/schemas'
    for stem, data in [('block-properties', properties), ('block-defaults', defaults),
                       ('block-registry-source', {'minecraft_version': '26.3', 'server': server,
                        'report_sha256': hashlib.sha256(args.report.read_bytes()).hexdigest(),
                        'blocks': len(report), 'states': sum(len(b['states']) for b in report.values())})]:
        (destination / f'{stem}-26.3.json').write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    print(f'Imported {len(report)} blocks')


if __name__ == '__main__':
    main()
