"""Build an immutable read snapshot, then publish under current authority and lease.

The read-only transaction never waits for a financial control or audit lock.
Publication is a separate short transaction; stale builders can neither publish
nor clear bytes already published by the current owner.
"""
import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction, connection
from django.db.models import BigIntegerField, Max
from django.db.models.functions import Cast
from .models import Export, Job
from .audit import record

MAX_ROWS = 100000
MAX_BYTES = 20 * 1024 * 1024


class StaleExportWorker(ValidationError):
    def __init__(self):
        super().__init__('This export job lease or generation is no longer current.', code='stale_job')


@dataclass(frozen=True)
class ExportSnapshot:
    export_id: object
    requester_id: object
    merchant_id: object
    definition_hash: str
    content: bytes
    cutoff_sequence: int
    generated_at: object
    cutoffs: dict


class BoundedCSV:
    def __init__(self):
        self.buffer = io.BytesIO()
        self.writer = csv.writer(self)

    def write(self, text):
        data = text.encode('utf-8')
        if self.buffer.tell() + len(data) > MAX_BYTES:
            raise ValidationError('Export byte limit exceeded.')
        self.buffer.write(data)
        return len(text)

    def row(self, values):
        self.writer.writerow(values)

    def bytes(self):
        return self.buffer.getvalue()


def safe_cell(value):
    text = str(value if value is not None else '')
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text


def _database_now():
    with connection.cursor() as cursor:
        cursor.execute('SELECT clock_timestamp()')
        return cursor.fetchone()[0]


def _definition(export):
    return hashlib.sha256(json.dumps({
        'requester': str(export.requester_id), 'merchant': str(export.merchant_id),
        'kind': export.kind, 'filters': export.filters,
    }, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def _same_live_claim(current, job, export, now):
    return bool(current and job and current.status == 'CLAIMED' and
        current.claim_generation == job.claim_generation and current.lease_owner == job.lease_owner and
        current.lease_until and current.lease_until > now and current.kind == 'export_build' and
        current.schema_version == 1 and current.policy_version == 'mprp_v1' and
        str(current.payload.get('export_id')) == str(export.pk) and current.merchant_id == export.merchant_id)


def _initial_claim(export_id, job):
    if job is None:
        raise StaleExportWorker()
    export = Export.objects.only('id', 'merchant_id').get(pk=export_id)
    current = Job.objects.filter(pk=job.pk).first()
    if not _same_live_claim(current, job, export, _database_now()):
        raise StaleExportWorker()


def _write_ledger(export, output, sequence):
    from accounting.models import JournalEntry
    output.row(['sequence', 'occurred_at UTC', 'source', 'purpose', 'account', 'side', 'amount USD cents'])
    rows = JournalEntry.objects.filter(journal__merchant=export.merchant, journal__sequence__lte=sequence).select_related('journal__source', 'account').order_by('journal__sequence', 'id')
    if rows.count() > MAX_ROWS:
        raise ValidationError('Export row limit exceeded.')
    for row in rows.iterator(chunk_size=500):
        output.row([row.journal.sequence, row.journal.occurred_at.isoformat(), safe_cell(row.journal.source.source_id), row.journal.purpose, row.account.code, row.side, row.amount_minor])


def _write_account(export, output):
    output.row(['email', 'active', 'merchant', 'role'])
    for index, member in enumerate(export.requester.memberships.select_related('merchant').order_by('created_at', 'id').iterator(chunk_size=500), 1):
        if index > MAX_ROWS:
            raise ValidationError('Export row limit exceeded.')
        output.row([safe_cell(export.requester.email), export.requester.is_active, safe_cell(member.merchant.name), member.role])


def _build_snapshot(export_id):
    """Return bounded bytes from one database snapshot; perform no writes or HTTP."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            cursor.execute("SET LOCAL statement_timeout = '120s'")
            cursor.execute('SELECT transaction_timestamp(), pg_current_snapshot()::text')
            snapshot_at, database_snapshot = cursor.fetchone()
        export = Export.objects.select_related('requester', 'merchant').get(pk=export_id)
        if export.status == 'READY':
            return None
        if export.status not in ('QUEUED', 'RETRYING'):
            raise ValidationError('This export is not eligible for generation.')
        from merchants.domain import require_merchant
        if not export.requester.is_active or not export.requester.verified or not export.requester.totp_confirmed:
            raise PermissionDenied('The requester no longer has account authority.')
        if export.kind == 'ledger' and export.merchant_id:
            require_merchant(export.requester, export.merchant_id)
        elif export.kind != 'account' or export.merchant_id is not None:
            raise ValidationError('Unsupported export kind or resource scope.')
        output = BoundedCSV()
        sequence = 0
        cutoffs = {'snapshot_at': snapshot_at.isoformat(), 'database_snapshot': database_snapshot}
        if export.kind == 'account':
            _write_account(export, output)
        else:
            from accounting.models import Journal, EconomicSource
            sequence = Journal.objects.filter(merchant=export.merchant).aggregate(n=Max('sequence'))['n'] or 0
            sources = EconomicSource.objects.filter(merchant=export.merchant)
            cutoffs.update({
                'economic_source_count': sources.count(),
                'economic_source_high_water': sources.aggregate(n=Max(Cast('payload__sequence', BigIntegerField())))['n'] or 0,
                'ledger_sequence': sequence,
            })
            _write_ledger(export, output, sequence)
        generated_at = _database_now()
        header = BoundedCSV()
        header.row(['MPRP SANDBOX', 'Simulated USD', 'generated_at UTC', generated_at.isoformat(), 'snapshot_at UTC', snapshot_at.isoformat(), 'cutoff sequence', sequence])
        content = header.bytes() + output.bytes()
        if len(content) > MAX_BYTES:
            raise ValidationError('Export byte limit exceeded.')
        return ExportSnapshot(export.pk, export.requester_id, export.merchant_id, _definition(export), content, sequence, generated_at, cutoffs)


def _lock_scope(export_id):
    """Use the same User -> Merchant -> Membership order as authority changes."""
    from accounts.models import User
    from merchants.models import Merchant, Membership
    hint = Export.objects.only('requester_id', 'merchant_id').get(pk=export_id)
    actor = User.objects.select_for_update().get(pk=hint.requester_id)
    merchant = Merchant.objects.select_for_update().get(pk=hint.merchant_id) if hint.merchant_id else None
    if merchant:
        list(Membership.objects.select_for_update().filter(user=actor, merchant=merchant).order_by('pk'))
    return hint, actor, merchant


def _assert_authority(actor, merchant):
    if not actor.is_active or not actor.verified or not actor.totp_confirmed:
        raise PermissionDenied('The requester no longer has account authority.')
    if merchant:
        from merchants.domain import require_merchant
        require_merchant(actor, merchant.pk)


def _publish_snapshot(snapshot, job):
    with transaction.atomic():
        hint, actor, merchant = _lock_scope(snapshot.export_id)
        _assert_authority(actor, merchant)
        current = Job.objects.select_for_update().filter(pk=job.pk).first()
        export = Export.objects.select_for_update().get(pk=snapshot.export_id)
        if export.requester_id != hint.requester_id or export.merchant_id != hint.merchant_id:
            raise StaleExportWorker()
        if not _same_live_claim(current, job, export, _database_now()):
            raise StaleExportWorker()
        # A surviving successful publication is immutable, including when a
        # duplicate job or an earlier generation eventually reaches this point.
        if export.status == 'READY':
            return False
        if export.status not in ('QUEUED', 'RETRYING') or _definition(export) != snapshot.definition_hash:
            raise ValidationError('The admitted export definition or state changed.')
        if snapshot.generated_at + timedelta(hours=24) <= _database_now():
            raise ValidationError('The generated snapshot expired before publication.')
        export.content = snapshot.content
        export.cutoff_sequence = snapshot.cutoff_sequence
        export.generated_at = snapshot.generated_at
        export.expires_at = snapshot.generated_at + timedelta(hours=24)
        export.filters = {**export.filters, 'snapshot': snapshot.cutoffs}
        export.status = 'READY'
        export.save(update_fields=['content', 'cutoff_sequence', 'generated_at', 'expires_at', 'filters', 'status'])
        record(actor, merchant, 'export.generated', export.pk, {
            'cutoff_sequence': snapshot.cutoff_sequence, 'job_id': str(job.pk),
            'claim_generation': job.claim_generation, 'source_cutoffs': snapshot.cutoffs,
        })
        # Audit-head contention also consumes the lease. Roll back the whole
        # publication if validity was consumed while taking those locks.
        if not _same_live_claim(current, job, export, _database_now()):
            raise StaleExportWorker()
        return True


def _record_failure(export_id, job, error):
    if job is None:
        return
    with transaction.atomic():
        hint, actor, merchant = _lock_scope(export_id)
        current = Job.objects.select_for_update().filter(pk=job.pk).first()
        export = Export.objects.select_for_update().get(pk=export_id)
        if export.requester_id != hint.requester_id or export.merchant_id != hint.merchant_id:
            return
        if not _same_live_claim(current, job, export, _database_now()):
            return
        # Never clear READY, EXPIRED, or another terminal winner, even when the
        # failed builder still owns a different duplicate job for this export.
        if export.status not in ('QUEUED', 'RETRYING'):
            return
        export.status = 'RETRYING' if current.attempts < 5 else 'FAILED'
        export.content = None
        export.save(update_fields=['status', 'content'])
        record(None, merchant, 'export.build_failed', export.pk, {
            'error_class': type(error).__name__, 'state': export.status,
            'job_id': str(job.pk), 'claim_generation': job.claim_generation,
        })


def build_export(export_id, *, job=None):
    """Publish only for the caller's explicit current durable job identity."""
    try:
        _initial_claim(export_id, job)
        snapshot = _build_snapshot(export_id)
        if snapshot is None:
            return False
        return _publish_snapshot(snapshot, job)
    except Exception as exc:
        try:
            _record_failure(export_id, job, exc)
        except Exception as failure:
            from .diagnostics import emit
            emit('export_failure_record_failed', error_class=type(failure).__name__)
        raise
