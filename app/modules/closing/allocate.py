"""金额按数量比分摊，整数分（人民币分）累计，最大余数法保证精确对账。"""

from app.errors import TrialError


def split_amount(amount_cents, weights):
    """把整数金额按整数权重摊到各份额，余额按最大余数补给，总和恒等于整批金额。"""
    if amount_cents < 0 or not weights:
        raise TrialError('ALLOCATION_NOT_POSSIBLE')
    if any(weight < 0 for weight in weights):
        raise TrialError('ALLOCATION_NOT_POSSIBLE')
    total_weight = sum(weights)
    if total_weight <= 0:
        raise TrialError('ALLOCATION_NOT_POSSIBLE')
    shares = [amount_cents * weight // total_weight for weight in weights]
    remainder = amount_cents - sum(shares)
    leftovers = sorted(
        ((amount_cents * weight % total_weight, index) for index, weight in enumerate(weights)),
        reverse=True,
    )
    for index in range(remainder):
        shares[leftovers[index][1]] += 1
    return shares
