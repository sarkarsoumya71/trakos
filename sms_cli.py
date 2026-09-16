"""Inspect a backup locally without contacting Telegram, Sheets or an LLM."""
import argparse
import json
import tempfile
from pathlib import Path

from sms_import import Ledger, MAX_BYTES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup', type=Path)
    args = parser.parse_args()
    with args.backup.open('rb') as source:
        payload = source.read(MAX_BYTES + 1)
    with tempfile.TemporaryDirectory(prefix='trakos-preview-') as directory:
        db = Ledger(Path(directory) / 'preview.sqlite3')
        report = db.import_xml(payload, owner=1)
        print(json.dumps(report, indent=2))
        print('Preview only. No live ledger or sheet was changed.')


if __name__ == '__main__':
    main()
