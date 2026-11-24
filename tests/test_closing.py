"""M8 成本关账：五条关账规则的验收测试。"""
import pytest

from app.main import create_app
from app.modules.closing.allocate import split_amount
from app.modules.closing.service import book, build_view, close_period
from app.modules.closing.storage import load
from app.errors import TrialError


def archive(client):
    return client.application.config['ARCHIVE']


def certify_dirty(client):
    return client.post('/api/batches', json={'sample': 'dirty'}).json['batch_id']


def invoice(client, **overrides):
    payload = {'lot': 'PJ-01', 'invoice_no': 'INV-1', 'period': '2026-11', 'kind': 'ACTUAL',
               'quantity_g': 5000, 'amount_cents': 100000, **overrides}
    return client.post('/api/closing/invoices', json=payload)


# ---- R1：领用对账通过并签发合格后，留档领用才计入成本 ----

def test_only_certified_issues_enter_cost(client):
    batch = certify_dirty(client)
    view = build_view(archive(client))
    # 未登记发票：合格领用仍被识别为待核实，而不是漏报。
    pj01 = next(row for row in view['lots'] if row['lot'] == 'PJ-01')
    assert pj01['status'] == 'MISSING_INVOICE' and pj01['issued_g'] == 5000
    pj02 = next(row for row in view['lots'] if row['lot'] == 'PJ-02')
    assert pj02['issued_g'] == 5000
    # 不可行计划留档后不签发合格，其领用不进成本。
    bad = {'sample': 'dirty'}
    bad['plan'] = {'as_of': '2026-11-22', 'target_g': 10000, 'step_g': 2500,
                   'dry_min_g': 8900, 'gel_low_sag': 195, 'gel_high_sag': 200}
    client.post('/api/batches', json=bad)
    view = build_view(archive(client))
    pj01 = next(row for row in view['lots'] if row['lot'] == 'PJ-01')
    assert pj01['issued_g'] == 5000  # 不合格批次的领用没有叠加
    assert batch


def test_verified_invoice_is_closeable_provisional_waits(client):
    certify_dirty(client)
    r = invoice(client, lot='PJ-01')
    assert r.status_code == 201 and r.json['created'] is True
    view = build_view(archive(client))
    pj01 = next(row for row in view['lots'] if row['lot'] == 'PJ-01')
    assert pj01['closeable'] and pj01['status'] == 'VERIFIED'
    # 同批仅暂估价：挂待核实，不允许先关。
    invoice(client, lot='PJ-02', invoice_no='INV-2', kind='PROVISIONAL', amount_cents=90000)
    view = build_view(archive(client))
    pj02 = next(row for row in view['lots'] if row['lot'] == 'PJ-02')
    assert not pj02['closeable'] and pj02['status'] == 'PROVISIONAL'
    assert all(line['estimated'] for line in pj02['lines'])
    assert (view['counts']['closeable'], view['counts']['waiting']) == (1, 1)


# ---- R2：同一来料批供多单，各单与余料金额同整批发票分毫不差 ----

def test_amount_splits_exactly_largest_remainder():
    # 等额权重时尾差按索引落在最后的份额，总和恒定。
    assert split_amount(100, [1, 1, 1]) == [33, 33, 34]
    assert sum(split_amount(777, [3, 5, 2, 1])) == 777
    assert split_amount(10, [7, 3]) == [7, 3]


def test_multi_order_and_remainder_reconcile_to_batch_invoice(client):
    batch_a = certify_dirty(client)
    # 再造一份不同计划的合格留档，使 PJ-01 供两个单。
    second = {'sample': 'dirty'}
    second['plan'] = {'as_of': '2026-11-22', 'target_g': 7500, 'step_g': 2500,
                      'dry_min_g': 6000, 'gel_low_sag': 140, 'gel_high_sag': 180}
    batch_b = client.post('/api/batches', json=second).json['batch_id']
    # PJ-01 发票 10000 g / 20000 分；两单各 5000 g，余料 0 g（发票量恰好用尽时不生余料行）。
    invoice(client, lot='PJ-01', quantity_g=10000, amount_cents=20000)
    view = build_view(archive(client))
    pj01 = next(row for row in view['lots'] if row['lot'] == 'PJ-01')
    refs = {line['ref']: line for line in pj01['lines']}
    assert set(refs) == {batch_a, batch_b}
    assert sum(line['amount_cents'] for line in pj01['lines']) == 20000
    # 发票 12000 g：多出 2000 g 余料，余料行与各单合计仍等于整批发票。
    ledger = load(archive(client))
    ledger['invoices'].clear()
    from app.modules.closing.storage import save
    save(archive(client), ledger)
    invoice(client, lot='PJ-01', quantity_g=12000, amount_cents=20000)
    view = build_view(archive(client))
    pj01 = next(row for row in view['lots'] if row['lot'] == 'PJ-01')
    assert sum(line['amount_cents'] for line in pj01['lines']) == 20000
    remainder = next(line for line in pj01['lines'] if line['ref'] == 'REMAINDER')
    assert remainder['quantity_g'] == 2000
    assert {line['ref'] for line in pj01['lines']} == {batch_a, batch_b, 'REMAINDER'}


def test_invoice_quantity_below_issued_blocks_close(client):
    certify_dirty(client)
    invoice(client, lot='PJ-01', quantity_g=4000, amount_cents=8000)
    view = build_view(archive(client))
    pj01 = next(row for row in view['lots'] if row['lot'] == 'PJ-01')
    assert pj01['status'] == 'INVOICE_SHORTAGE' and not pj01['closeable']
    assert pj01['lines'] == []


# ---- R3：晚到发票涉及已封账月份，旧月原样，差额落开放月 ----

def test_close_freezes_verified_keeps_provisional_out(client):
    certify_dirty(client)
    invoice(client, lot='PJ-01', amount_cents=100000)
    invoice(client, lot='PJ-02', invoice_no='INV-2', kind='PROVISIONAL', amount_cents=90000)
    snapshot = client.post('/api/closing/close',
                           json={'period': '2026-11', 'next_period': '2026-12'}).json
    assert snapshot['reported_yuan'] == '1000.00'          # 仅已核实成本进旧月
    assert snapshot['provisional_yuan'] == '900.00'        # 暂估单独挂账
    view = build_view(archive(client))
    assert view['open_period'] == '2026-12'
    assert view['closed_periods'][0]['period'] == '2026-11'


def test_late_invoice_delta_lands_in_open_month_closed_month_untouched(client):
    certify_dirty(client)
    invoice(client, lot='PJ-01', amount_cents=100000)
    client.post('/api/closing/close', json={'period': '2026-11', 'next_period': '2026-12'})
    # 12 月才到的 PJ-02 正式发票（业务发生在 11 月）：旧月不动，全额进开放月。
    late = invoice(client, lot='PJ-02', invoice_no='INV-2', period='2026-11', amount_cents=90000)
    event = late.json['event']
    assert event['late'] and event['posted_period'] == '2026-12'
    view = build_view(archive(client))
    assert view['closed_periods'][0]['reported_cents'] == 100000
    assert view['open_month']['actual_delta_cents'] == 90000
    pj02 = next(row for row in view['lots'] if row['lot'] == 'PJ-02')
    assert pj02['closeable'] and pj02['status'] == 'VERIFIED'


# ---- R4：发票更正只补差额，重试不重复计费 ----

def test_correction_posts_only_delta_and_retry_is_idempotent(client):
    certify_dirty(client)
    invoice(client, amount_cents=100000)
    correction = invoice(client, version=2, amount_cents=10500)
    event = correction.json['event']
    assert correction.status_code == 201 and event['delta_cents'] == -89500
    view = build_view(archive(client))
    # 开放月实际差额 = 首单 100000 + 更正差额 -89500，而不是 100000 + 10500。
    assert view['open_month']['actual_delta_cents'] == 10500
    pj01 = next(row for row in view['lots'] if row['lot'] == 'PJ-01')
    assert pj01['invoice_amount_cents'] == 10500
    # 同版本同载荷重试：幂等返回，不产生第二条事件。
    retry = invoice(client, version=2, amount_cents=10500)
    assert retry.status_code == 200 and retry.json['created'] is False
    assert len(load(archive(client))['invoices']) == 2
    assert build_view(archive(client))['open_month']['actual_delta_cents'] == 10500


def test_correction_after_close_carries_delta_to_open_month(client):
    certify_dirty(client)
    invoice(client, amount_cents=100000)
    client.post('/api/closing/close', json={'period': '2026-11', 'next_period': '2026-12'})
    invoice(client, period='2026-11', version=2, amount_cents=108000)
    view = build_view(archive(client))
    assert view['closed_periods'][0]['reported_cents'] == 100000   # 旧月保持原样
    assert view['open_month']['actual_delta_cents'] == 8000        # 仅差额 80.00 落开放月


def test_provisional_then_actual_keeps_current_view_consistent(client):
    certify_dirty(client)
    invoice(client, kind='PROVISIONAL', amount_cents=90000)
    assert invoice(client, version=2, kind='ACTUAL', amount_cents=10000).status_code == 201
    view = build_view(archive(client))
    pj01 = next(row for row in view['lots'] if row['lot'] == 'PJ-01')
    assert pj01['status'] == 'VERIFIED' and pj01['invoice_amount_cents'] == 10000
    # 暂估转正：旧暂估被冲回（净额 0），开放月只留正式金额。
    assert view['open_month']['provisional_delta_cents'] == 0
    assert view['open_month']['actual_delta_cents'] == 10000


def test_conflicting_version_rejected(client):
    certify_dirty(client)
    invoice(client, amount_cents=100000)
    conflict = invoice(client, version=1, amount_cents=99999)
    assert (conflict.status_code, conflict.json['code']) == (422, 'INVOICE_VERSION_CONFLICT')


# ---- R5：关账页面与 API 可查金额和暂估状态 ----

def test_closing_api_serves_amounts_and_status(client):
    certify_dirty(client)
    invoice(client, amount_cents=100000)
    reply = client.get('/api/closing')
    assert reply.status_code == 200
    body = reply.json
    assert {'open_period', 'closeable', 'waiting', 'closed_periods', 'open_month', 'counts'} <= set(body)
    row = body['closeable'][0]
    assert row['invoice_amount_yuan'] == '1000.00' and row['status'] == 'VERIFIED'
    assert row['lines'][0]['amount_yuan']


def test_page_renders_closing_stage(client):
    certify_dirty(client)
    page = client.get('/trial/closing')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert '成本关账台' in html and 'PJ-01' in html and '可先关' in html and '还要等' in html


def test_period_and_payload_validation(client):
    certify_dirty(client)
    bad_period = invoice(client, period='2026-13')
    assert (bad_period.status_code, bad_period.json['code']) == (400, 'PERIOD_FORMAT_INVALID')
    bad_amount = invoice(client, amount_cents=0)
    assert (bad_amount.status_code, bad_amount.json['code']) == (422, 'INVOICE_AMOUNT_INVALID')
    with pytest.raises(TrialError):
        close_period(archive(client), {'period': '2026-11', 'next_period': '2026-11'})


def test_close_requires_matching_open_period(client):
    certify_dirty(client)
    invoice(client)
    reply = client.post('/api/closing/close', json={'period': '2026-10', 'next_period': '2026-11'})
    assert (reply.status_code, reply.json['code']) == (422, 'PERIOD_NOT_OPEN')


def test_yuan_helper_round_trip():
    from app.modules.closing.service import yuan
    assert yuan(105000) == '1050.00'
    assert yuan(5) == '0.05'
