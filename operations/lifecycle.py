"""Conservative closure admission from authoritative records and evidence."""
from datetime import timezone as datetime_timezone
from django.db.models import Sum


def closure_blockers(merchant):
    from accounting.domain import balances
    from accounting.models import AccountingPeriod, EconomicSource
    from disputes.models import Dispute
    from payments.models import Refund
    from reconciliation.domain import line_remaining, source_remaining
    from reconciliation.models import ReconciliationException, StatementLine
    from settlements.models import Payout, SettlementBatch
    from .models import Operation

    blockers = []
    if any(balances(merchant).values()):
        blockers.append('Every ledger balance and deficit must be zero.')
    if Operation.objects.filter(merchant=merchant).exclude(status__in=['SUCCEEDED', 'FAILED', 'CANCELLED']).exists():
        blockers.append('All accepted economic operations must have a terminal authoritative outcome.')
    if Refund.objects.filter(merchant=merchant, reserved=True).exists():
        blockers.append('Active refund reservations remain.')
    if Payout.objects.filter(merchant=merchant).exclude(status__in=['PAID', 'DECLINED', 'CANCELLED']).exists() or Payout.objects.filter(merchant=merchant, reserved=True).exists():
        blockers.append('Active payout proposals or reservations remain.')
    if Dispute.objects.filter(merchant=merchant, state__in=['OPEN', 'RESPONDED']).exists():
        blockers.append('Open disputes must be resolved.')
    if AccountingPeriod.objects.filter(merchant=merchant).exclude(state='CLOSED').exists():
        blockers.append('Every existing accounting period must be closed.')
    if ReconciliationException.objects.filter(merchant=merchant).exclude(state__in=['RESOLVED', 'ACCEPTED_NONFINANCIAL']).exists():
        blockers.append('Unresolved reconciliation exceptions remain.')

    # Absence of an exception row is not evidence of completeness. Independently
    # compare every authoritative source and every admitted statement line.
    for source in EconomicSource.objects.filter(merchant=merchant).iterator(chunk_size=250):
        month = source.occurred_at.astimezone(datetime_timezone.utc).date().replace(day=1)
        if not AccountingPeriod.objects.filter(merchant=merchant, month=month, state='CLOSED').exists():
            blockers.append('An economic activity month lacks a completed close.')
            break
    for source in EconomicSource.objects.filter(merchant=merchant).iterator(chunk_size=250):
        if not source.recognized and source.event_type != 'BATCH_ZERO':
            blockers.append('Unrecognized authoritative economic sources remain.')
            break
        kinds = ('PROCESSOR_TRANSFER', 'BANK') if source.event_type in ('SETTLEMENT', 'PAYOUT') else ('PROCESSOR_TRANSFER',) if source.event_type == 'BATCH_ZERO' else ('PROCESSOR_ECONOMIC',)
        if any(source_remaining(source, kind) != 0 or (source.amount_minor == 0 and not source.allocations.filter(line__kind=kind, group__status='MATCHED').exists()) for kind in kinds):
            blockers.append('Authoritative sources lack complete processor or bank statement evidence.')
            break
    for line in StatementLine.objects.filter(merchant=merchant).iterator(chunk_size=250):
        if line_remaining(line) != 0 or (line.amount_minor == 0 and not line.allocations.filter(group__status='MATCHED').exists()):
            blockers.append('Admitted statement lines remain unmatched.')
            break
    for batch in SettlementBatch.objects.filter(merchant=merchant).iterator(chunk_size=250):
        detail = [source for source in batch.sources if (source.get('event_type') or source.get('kind')) not in ('SETTLEMENT', 'SETTLEMENT_TRANSFER')]
        if sum(source.get('amount_minor', 0) for source in detail) != batch.net_minor:
            blockers.append('Settlement batch detail does not reconcile to its net.')
            break
        transfer = getattr(batch, 'transfer', None)
        if batch.net_minor and (transfer is None or transfer.state != 'SUCCEEDED' or transfer.amount_minor != batch.net_minor):
            blockers.append('A settlement batch lacks its exact successful transfer disposition.')
            break
        if not batch.net_minor and transfer is not None:
            blockers.append('A zero batch unexpectedly has a transfer.')
            break
    return list(dict.fromkeys(blockers))
