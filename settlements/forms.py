from django import forms
from payments.forms import ActionForm, StrictForm, parse_amount


class BeneficiaryForm(StrictForm):
    token = forms.CharField(max_length=64, label='Synthetic beneficiary token', help_text='Only the acct_demo token issued to this merchant is accepted.')


class PayoutForm(ActionForm):
    amount = forms.CharField(max_length=16, widget=forms.TextInput(attrs={'inputmode': 'decimal'}))
    beneficiary_version = forms.IntegerField(min_value=1)
    reason = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={'rows': 3}))

    def clean_amount(self):
        return parse_amount(self.cleaned_data['amount'])


class ApprovalForm(StrictForm):
    action_key = forms.RegexField(regex=r'^[A-Za-z0-9_-]{32,128}$', min_length=32, max_length=128, widget=forms.HiddenInput)
    proposal_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    step_up_reference = forms.CharField(max_length=128, required=True, widget=forms.HiddenInput)
