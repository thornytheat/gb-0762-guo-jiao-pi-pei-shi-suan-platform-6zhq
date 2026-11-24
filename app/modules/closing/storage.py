"""成本关账台账：发票入账事件、关账快照的原子持久化。"""
import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.errors import TrialError
from app.modules.batches.seal import canonical

PERIOD_PATTERN = re.compile(r'\d{4}-(?:0[1-9]|1[0-2])')


def ledger_path(root):
    return Path(root) / 'cost-ledger.json'


def empty_ledger():
    return {'open_period': None, 'closed': {}, 'invoices': []}


def load(root):
    path = ledger_path(root)
    if not path.exists():
        return empty_ledger()
    try:
        ledger = json.loads(path.read_bytes())
    except json.JSONDecodeError:
        raise TrialError('LEDGER_CORRUPT', 500) from None
    for key, default in empty_ledger().items():
        ledger.setdefault(key, default)
    return ledger


def save(root, ledger):
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = ledger_path(directory)
    with NamedTemporaryFile(dir=directory, delete=False) as file:
        temp = Path(file.name)
        try:
            file.write(canonical(ledger))
            file.flush()
            os.fsync(file.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def validate_period(period):
    if not isinstance(period, str) or not PERIOD_PATTERN.fullmatch(period):
        raise TrialError('PERIOD_FORMAT_INVALID', 400)
    return period
