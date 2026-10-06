"""Credit accounting retained from the notebook's total-load calculation."""
class CreditLedger:
    def __init__(self, config):
        self.config = config
        self.rows = {}

    def state(self, tenant, now):
        limits = self.config['tenant_limits'][tenant]
        cap = float(limits.get('burst_credit_us', 0))
        row = self.rows.setdefault(tenant, dict(balance=cap, updated_us=now,
                                                reserved_us=0.0, penalties_us=0.0, refunds_us=0.0))
        refill = float(limits.get('burst_refill_us', 0))
        row['balance'] = min(cap, row['balance'] + max(0, now-row['updated_us']) * refill /
                             self.config.get('burst_refill_window_us', 1))
        row['updated_us'] = now
        return row

    def required(self, tenant, active_work, charge):
        limit = float(self.config['tenant_limits'][tenant].get('max_work_us', 0))
        return max(0.0, active_work+charge-limit)

    def reserve(self, tenant, amount, now):
        row = self.state(tenant, now)
        row['balance'] -= amount
        row['reserved_us'] += amount

    def settle(self, request, outcome, now):
        tenant = request['tenant']
        row = self.state(tenant, now)
        if outcome == 'success':
            row['balance'] += request.get('burst_reserved_us', 0)
            row['refunds_us'] += request.get('burst_reserved_us', 0)
        else:
            penalty = float(self.config['tenant_limits'][tenant].get('failure_penalty_us', 0))
            row['balance'] -= penalty
            row['penalties_us'] += penalty
