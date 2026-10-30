from django import forms
from merchants.models import Merchant
from .models import StatementImport

class StrictForm(forms.Form):
    def clean(self):
        data=super().clean()
        allowed={self.add_prefix(name) for name in self.fields}|{'csrfmiddlewaretoken'}
        if (set(self.data)|set(self.files))-allowed:raise forms.ValidationError('Unsupported form fields. Refresh and try again.')
        return data


class StatementUploadForm(StrictForm):
    merchant=forms.ModelChoiceField(queryset=Merchant.objects.order_by('name'))
    kind=forms.ChoiceField(choices=StatementImport.KINDS)
    account_id=forms.CharField(max_length=128,label='Synthetic account identifier')
    statement_id=forms.CharField(max_length=128)
    manifest_id=forms.CharField(max_length=128)
    period_start=forms.DateTimeField(help_text='Inclusive UTC instant, for example 2026-10-01T00:00:00Z')
    period_end=forms.DateTimeField(help_text='Exclusive UTC instant, at most 31 days after start')
    opening_balance_minor=forms.IntegerField(label='Opening balance or accumulator in USD cents')
    closing_balance_minor=forms.IntegerField(label='Closing balance or accumulator in USD cents')
    declared_row_count=forms.IntegerField(min_value=0,max_value=10000)
    declared_net_minor=forms.IntegerField(label='Signed net in USD cents')
    file=forms.FileField(help_text='UTF-8 CSV, at most 5 MiB and 10000 rows')
    def clean_file(self):
        value=self.cleaned_data['file']
        if value.size>5*1024*1024:raise forms.ValidationError('The file exceeds 5 MiB.')
        if not value.name.lower().endswith('.csv') or getattr(value,'content_type','text/csv').split(';')[0] not in ('text/csv','application/csv','text/plain','application/octet-stream'):
            raise forms.ValidationError('Select a plain UTF-8 .csv file with a compatible CSV content type.')
        return value
    def clean(self):
        values=super().clean()
        if values.get('kind')!='BANK' and values.get('merchant') and values.get('account_id')!=str(values['merchant'].provider_account_id):
            self.add_error('account_id','Use the selected merchant provider account.')
        return values

class VersionForm(StrictForm):
    expected_version=forms.IntegerField(min_value=1,widget=forms.HiddenInput)
    action_key=forms.CharField(min_length=32,max_length=128,widget=forms.HiddenInput)

class ReasonForm(VersionForm):
    reason=forms.CharField(max_length=2000,widget=forms.Textarea)

class ExceptionActionForm(StrictForm):
    reason=forms.CharField(max_length=2000,required=False,widget=forms.Textarea)
    proof=forms.CharField(max_length=500,required=False,label='Linked nonfinancial proof')

class MatchForm(StrictForm):
    line_id=forms.UUIDField(label='Statement evidence line')
    source_id=forms.UUIDField(label='Authoritative economic source')
    amount_minor=forms.IntegerField(label='Signed allocated USD cents')
    reason=forms.CharField(max_length=500,required=False)

class AdjustmentForm(StrictForm):
    source_id=forms.UUIDField(label='Authenticated correction source')
    rationale=forms.CharField(max_length=2000,widget=forms.Textarea)

class SimulatorStatementForm(StrictForm):
    merchant=forms.ModelChoiceField(queryset=Merchant.objects.order_by('name'))
    kind=forms.ChoiceField(choices=StatementImport.KINDS)
    period_start=forms.DateTimeField(help_text='Inclusive UTC instant')
    period_end=forms.DateTimeField(help_text='Exclusive UTC instant; the interval must have ended')

class ApprovalDecisionForm(StrictForm):
    action_key=forms.RegexField(regex=r'^[A-Za-z0-9_-]{32,128}$',widget=forms.HiddenInput)
    expected_version=forms.IntegerField(min_value=1,widget=forms.HiddenInput)
    proposal_hash=forms.RegexField(regex=r'^[a-f0-9]{64}$',widget=forms.HiddenInput)
