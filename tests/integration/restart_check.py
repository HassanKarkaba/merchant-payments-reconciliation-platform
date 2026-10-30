"""Compare authoritative financial state before and after a coherent full restart."""
import os,sys,json,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django
django.setup()
if not os.environ.get('MPRP_VERIFY_PROJECT','').startswith('mprp_verify_'):raise SystemExit('Verification project required')
from django.db import transaction,connection
from django.db.models import Sum
from merchants.models import Merchant
from accounting.models import Journal,JournalEntry
from accounting.domain import balances
from operations.models import Operation
from operations import provider
from operations.recovery import _provider_inventory
deadline=time.monotonic()+120
while Operation.objects.filter(status__in=['QUEUED','DISPATCHING','UNKNOWN']).exists():
    if time.monotonic()>=deadline:raise RuntimeError('Accepted financial work did not become terminal before restart observation')
    time.sleep(.2)
with transaction.atomic():
    with connection.cursor() as cursor:cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
    data={'merchants':[{ 'id':str(m.pk),'sequence':m.ledger_sequence,'balances':balances(m)} for m in Merchant.objects.order_by('pk')], 'journals':Journal.objects.count(),'entries':JournalEntry.objects.count(),'operations':list(Operation.objects.order_by('id').values('id','provider_key','kind','status'))}
operations,_=_provider_inventory()
data['provider_keys']=sorted((item['operation_key'],item.get('state')) for item in operations)
print(json.dumps(data,default=str,sort_keys=True,separators=(',',':')))
