"""成本关账领域服务。

规则落点：
- 仅留档批次中试配签发合格（can_trial 且库存平衡无缺口）的领用才计入成本。
- 同一来料批供多单：按各单克重比分摊整批发票金额，余料独占尾差行，合计与发票分毫不差。
- 暂估发票只挂开放月，不进已封账月份的已核实成本；晚到发票差额落入开放月。
- 发票更正按 invoice_no + version 记账，只补相对上次入账的差额；同版本重试幂等。
"""
from datetime import datetime, timezone

from app.errors import TrialError
from app.modules.batches.seal import canonical, sha
from app.modules.batches.storage import history, read
from app.modules.certificate.service import issue
from .allocate import split_amount
from .storage import load, save, validate_period

KINDS = ('ACTUAL', 'PROVISIONAL')
REMAINDER = 'REMAINDER'


def yuan(cents):
    sign = '-' if cents < 0 else ''
    return f'{sign}{abs(cents) / 100:.2f}'


def _positive_int(value, code):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TrialError(code)
    return value


def _amount_cents(payload):
    if 'amount_cents' in payload:
        return _positive_int(payload['amount_cents'], 'INVOICE_AMOUNT_INVALID')
    value = payload.get('amount_yuan')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise TrialError('INVOICE_AMOUNT_INVALID')
    return round(value * 100)


def _qualified_issues(archive):
    """返回 [(batch_id, lot, grams)]，只含对账通过并签发合格的留档批次。"""
    issues = []
    for summary in history(archive):
        record = read(archive, summary['batch_id'])
        if not issue(record)['can_trial']:
            continue
        for row in record['stages']['stock']['rows']:
            if row['issued_g'] > 0:
                issues.append((record['batch_id'], row['lot'], row['issued_g']))
    return issues


def _current_invoices(ledger):
    """每个发票号取最新版本，返回 {invoice_no: entry}。"""
    latest = {}
    for event in ledger['invoices']:
        previous = latest.get(event['invoice_no'])
        if previous is None or event['version'] > previous['version']:
            latest[event['invoice_no']] = event
    return latest


def _lot_invoice_state(ledger):
    """汇总每个来料批当前发票：状态 ACTUAL / PROVISIONAL / MISSING 与总量总额。"""
    lots = {}
    for event in _current_invoices(ledger).values():
        bucket = lots.setdefault(event['lot'], {'state': 'ACTUAL', 'quantity_g': 0, 'amount_cents': 0,
                                                'invoice_nos': []})
        if event['kind'] == 'PROVISIONAL':
            bucket['state'] = 'PROVISIONAL'
        bucket['quantity_g'] += event['quantity_g']
        bucket['amount_cents'] += event['amount_cents']
        bucket['invoice_nos'].append(event['invoice_no'])
    return lots


def book(root, payload):
    """登记一张（或更正一张）发票，返回事件与是否新入账。"""
    ledger = load(root)
    try:
        lot = payload['lot']
        invoice_no = payload['invoice_no']
        period = validate_period(payload['period'])
        kind = payload['kind']
    except KeyError:
        raise TrialError('INVOICE_FIELDS_REQUIRED') from None
    if not isinstance(lot, str) or not lot.strip() or not isinstance(invoice_no, str) or not invoice_no.strip():
        raise TrialError('INVOICE_FIELDS_REQUIRED')
    if kind not in KINDS:
        raise TrialError('INVOICE_KIND_INVALID')
    quantity_g = _positive_int(payload.get('quantity_g'), 'INVOICE_QUANTITY_INVALID')
    amount_cents = _amount_cents(payload)

    chain = [event for event in ledger['invoices'] if event['invoice_no'] == invoice_no]
    previous = max(chain, key=lambda event: event['version'], default=None)
    version = payload.get('version', (previous['version'] + 1 if previous else 1))
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise TrialError('INVOICE_VERSION_INVALID')
    if previous is not None:
        if previous['lot'] != lot:
            raise TrialError('INVOICE_LOT_CONFLICT')
        if version <= previous['version']:
            # 同版本重试：载荷一致则幂等返回，绝不重复计费；不一致则拒绝。
            same = (previous['period'] == period and previous['kind'] == kind
                    and previous['quantity_g'] == quantity_g and previous['amount_cents'] == amount_cents)
            if version == previous['version'] and same:
                return previous, False
            raise TrialError('INVOICE_VERSION_CONFLICT')
    elif version != 1:
        raise TrialError('INVOICE_VERSION_INVALID')

    if ledger['open_period'] is None:
        ledger['open_period'] = period
    open_period = ledger['open_period']
    posted_period = period if period == open_period else open_period
    delta_cents = amount_cents if previous is None else amount_cents - previous['amount_cents']
    event = {
        'event_id': sha(canonical(['cost-event', invoice_no, version, period, kind, quantity_g, amount_cents,
                                   posted_period, datetime.now(timezone.utc).isoformat(timespec='microseconds')])),
        'invoice_no': invoice_no, 'lot': lot, 'period': period, 'posted_period': posted_period,
        'version': version, 'kind': kind, 'quantity_g': quantity_g, 'amount_cents': amount_cents,
        'delta_cents': delta_cents, 'late': period < open_period,
        'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    ledger['invoices'].append(event)
    save(root, ledger)
    return event, True


def _lot_view(lot, orders, invoice):
    """生成单个来料批的关账视图，orders 为 [(batch_id, grams)]。"""
    issued_g = sum(grams for _, grams in orders)
    if invoice is None:
        return {'lot': lot, 'status': 'MISSING_INVOICE', 'closeable': False, 'reason': '发票未到，暂不能关',
                'issued_g': issued_g, 'invoice_quantity_g': 0, 'invoice_amount_cents': 0,
                'invoice_amount_yuan': yuan(0), 'lines': []}
    if invoice['quantity_g'] < issued_g:
        return {'lot': lot, 'status': 'INVOICE_SHORTAGE', 'closeable': False,
                'reason': f"发票量 {invoice['quantity_g']} g 小于合格领用 {issued_g} g，整批对账未平",
                'issued_g': issued_g, 'invoice_quantity_g': invoice['quantity_g'],
                'invoice_amount_cents': invoice['amount_cents'],
                'invoice_amount_yuan': yuan(invoice['amount_cents']), 'lines': []}
    weights = [grams for _, grams in orders]
    remainder_g = invoice['quantity_g'] - issued_g
    if remainder_g > 0:
        weights.append(remainder_g)
    shares = split_amount(invoice['amount_cents'], weights)
    lines = [{'ref': batch_id, 'quantity_g': grams, 'amount_cents': cents, 'amount_yuan': yuan(cents),
              'estimated': invoice['state'] == 'PROVISIONAL'}
             for (batch_id, grams), cents in zip(orders, shares)]
    if remainder_g > 0:
        cents = shares[-1]
        lines.append({'ref': REMAINDER, 'quantity_g': remainder_g, 'amount_cents': cents,
                      'amount_yuan': yuan(cents), 'estimated': invoice['state'] == 'PROVISIONAL'})
    assert sum(line['amount_cents'] for line in lines) == invoice['amount_cents']
    if not orders:
        status, closeable, reason = 'IDLE', False, '尚无合格领用，金额留在余料'
    elif invoice['state'] == 'PROVISIONAL':
        status, closeable, reason = 'PROVISIONAL', False, '仅收到暂估价，等齐正式发票后再关'
    else:
        status, closeable, reason = 'VERIFIED', True, '发票已核实且领用对账通过，可先关'
    return {'lot': lot, 'status': status, 'closeable': closeable, 'reason': reason, 'issued_g': issued_g,
            'invoice_quantity_g': invoice['quantity_g'], 'invoice_amount_cents': invoice['amount_cents'],
            'invoice_amount_yuan': yuan(invoice['amount_cents']), 'lines': lines}


def _period_movements(ledger):
    """开放月内按事件轧差汇总；版本间类型变更先冲回旧类型，重试事件不重复计费。"""
    open_period = ledger['open_period']
    movements = {'actual_delta_cents': 0, 'provisional_delta_cents': 0}
    if open_period is None:
        return movements
    chain = {}
    for event in ledger['invoices']:
        chain.setdefault(event['invoice_no'], []).append(event)
    for events in chain.values():
        for event in sorted(events, key=lambda item: item['version']):
            if event['posted_period'] != open_period:
                continue
            bucket = 'actual_delta_cents' if event['kind'] == 'ACTUAL' else 'provisional_delta_cents'
            movements[bucket] += event['amount_cents']
            previous = next((old for old in reversed(events) if old['version'] < event['version']), None)
            if previous is not None:
                old_bucket = 'actual_delta_cents' if previous['kind'] == 'ACTUAL' else 'provisional_delta_cents'
                movements[old_bucket] -= previous['amount_cents']
    return movements


def build_view(root):
    """关账页面数据：分批可关/待核实判定、分摊行、已封账月份冻结数与开放月差额。"""
    ledger = load(root)
    issues = _qualified_issues(root)
    orders_by_lot = {}
    for batch_id, lot, grams in issues:
        orders_by_lot.setdefault(lot, []).append((batch_id, grams))
    invoices = _lot_invoice_state(ledger)
    lots = sorted(set(orders_by_lot) | set(invoices))
    lot_views = [_lot_view(lot, orders_by_lot.get(lot, []), invoices.get(lot)) for lot in lots]
    movements = _period_movements(ledger)
    closed = [ledger['closed'][period] for period in sorted(ledger['closed'])]
    return {
        'open_period': ledger['open_period'],
        'closeable': [view for view in lot_views if view['closeable']],
        'waiting': [view for view in lot_views if not view['closeable']],
        'lots': lot_views,
        'closed_periods': closed,
        'open_month': {
            'period': ledger['open_period'],
            'actual_delta_cents': movements['actual_delta_cents'],
            'actual_delta_yuan': yuan(movements['actual_delta_cents']),
            'provisional_delta_cents': movements['provisional_delta_cents'],
            'provisional_delta_yuan': yuan(movements['provisional_delta_cents']),
        },
        'counts': {'lots': len(lot_views), 'closeable': sum(view['closeable'] for view in lot_views),
                   'waiting': sum(not view['closeable'] for view in lot_views)},
    }


def _invoices_as_of(ledger, period):
    """截至某账期封账时点可见的最新发票：晚到发票（posted_period 在其后）不进旧月快照。"""
    latest = {}
    for event in ledger['invoices']:
        if event['posted_period'] > period:
            continue
        previous = latest.get(event['invoice_no'])
        if previous is None or event['version'] > previous['version']:
            latest[event['invoice_no']] = event
    lots = {}
    for event in latest.values():
        bucket = lots.setdefault(event['lot'], {'state': 'ACTUAL', 'quantity_g': 0, 'amount_cents': 0})
        if event['kind'] == 'PROVISIONAL':
            bucket['state'] = 'PROVISIONAL'
        bucket['quantity_g'] += event['quantity_g']
        bucket['amount_cents'] += event['amount_cents']
    return lots


def close_period(root, payload):
    """封账：冻结当月已核实成本与暂估挂账，开放月切到下一账期；旧月金额之后不再变动。"""
    ledger = load(root)
    period = validate_period(payload.get('period'))
    next_period = validate_period(payload.get('next_period'))
    if ledger['open_period'] is None:
        raise TrialError('NO_OPEN_PERIOD')
    if period != ledger['open_period']:
        raise TrialError('PERIOD_NOT_OPEN')
    if next_period <= period:
        raise TrialError('NEXT_PERIOD_INVALID')
    if period in ledger['closed']:
        raise TrialError('PERIOD_ALREADY_CLOSED')

    issues = _qualified_issues(root)
    orders_by_lot = {}
    for batch_id, lot, grams in issues:
        orders_by_lot.setdefault(lot, []).append((batch_id, grams))
    # 只按封账时点已入账的发票冻结；晚到发票差额留在开放月，旧月保持原样。
    invoices = _invoices_as_of(ledger, period)
    lines, reported_cents, provisional_cents = [], 0, 0
    for lot, invoice in invoices.items():
        orders = orders_by_lot.get(lot, [])
        issued_g = sum(grams for _, grams in orders)
        if invoice['quantity_g'] < issued_g:
            continue
        weights = [grams for _, grams in orders]
        remainder_g = invoice['quantity_g'] - issued_g
        if remainder_g > 0:
            weights.append(remainder_g)
        shares = split_amount(invoice['amount_cents'], weights)
        refs = [batch_id for batch_id, _ in orders]
        if remainder_g > 0:
            refs.append(REMAINDER)
        for ref, grams, cents in zip(refs, weights, shares):
            lines.append({'lot': lot, 'ref': ref, 'quantity_g': grams, 'amount_cents': cents,
                          'amount_yuan': yuan(cents), 'estimated': invoice['state'] == 'PROVISIONAL'})
        if invoice['state'] == 'PROVISIONAL':
            provisional_cents += invoice['amount_cents']
        else:
            reported_cents += invoice['amount_cents']
    snapshot = {'period': period, 'closed_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'reported_cents': reported_cents, 'reported_yuan': yuan(reported_cents),
                'provisional_cents': provisional_cents, 'provisional_yuan': yuan(provisional_cents),
                'lines': lines}
    ledger['closed'][period] = snapshot
    ledger['open_period'] = next_period
    save(root, ledger)
    return snapshot
