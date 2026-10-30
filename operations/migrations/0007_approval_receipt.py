import uuid
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies=[('operations','0006_inbox_policy_version_inbox_schema_version_and_more')]
    operations=[
        migrations.AlterField(model_name='notification',name='recipient',field=models.ForeignKey(db_constraint=False,on_delete=django.db.models.deletion.PROTECT,to=settings.AUTH_USER_MODEL)),
        migrations.CreateModel(name='ApprovalReceipt',fields=[
            ('id',models.UUIDField(default=uuid.uuid4,editable=False,primary_key=True,serialize=False)),
            ('kind',models.CharField(max_length=64)),
            ('resource_id',models.UUIDField()),
            ('action_key',models.CharField(max_length=128)),
            ('fingerprint',models.CharField(max_length=64)),
            ('result_ref',models.UUIDField()),
            ('result_model',models.CharField(max_length=64)),
            ('schema_version',models.PositiveSmallIntegerField(default=1)),
            ('policy_version',models.CharField(default='mprp_v1',max_length=20)),
            ('created_at',models.DateTimeField(default=django.utils.timezone.now)),
            ('actor',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,related_name='approval_receipts',to=settings.AUTH_USER_MODEL)),
            ('merchant',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to='merchants.merchant')),
        ],options={'constraints':[models.UniqueConstraint(fields=('actor','merchant','kind','resource_id','action_key'),name='approval_receipt_identity')]}),
    ]
