import re
import unicodedata
from django import forms
from django.core.exceptions import ValidationError


AMOUNT = re.compile(r'^[0-9]+(?:\.[0-9]{1,2})?$')
SCENARIOS = [('tok_demo_success', 'Successful payment'), ('tok_demo_decline', 'Payment declined'), ('tok_demo_timeout_after_commit', 'Lost response after processing'), ('tok_demo_requires_review', 'Requires review — no financial effect')]


def parse_amount(value, minimum=1, maximum=100000000):
    value = value.strip()
    if not AMOUNT.fullmatch(value) or len(value) > 16:
        raise ValidationError('Enter USD using digits and at most two decimal places.')
    whole, _, fraction = value.partition('.')
    amount = int(whole) * 100 + int((fraction + '00')[:2])
    if amount < minimum or amount > maximum:
        raise ValidationError(f'Amount must be between ${minimum / 100:.2f} and ${maximum / 100:.2f}.')
    return amount


class StrictForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.unknown_fields = set(self.data) - set(self.fields) - {'csrfmiddlewaretoken', 'submit'} if self.is_bound else set()

    def clean(self):
        data = super().clean()
        if self.unknown_fields:
            raise ValidationError('This form contains unsupported fields. Reload the page and try again.')
        return data


class LinkForm(StrictForm):
    amount = forms.CharField(max_length=16, help_text='Simulated USD, $1.00 to $1,000,000.00', widget=forms.TextInput(attrs={'inputmode': 'decimal'}))
    capture_mode = forms.ChoiceField(choices=[('AUTOMATIC', 'Automatic capture'), ('MANUAL', 'Manual final capture')])
    expiry_minutes = forms.IntegerField(min_value=5, max_value=10080, initial=1440, help_text='5 minutes to 7 days')

    def clean_amount(self):
        return parse_amount(self.cleaned_data['amount'], minimum=100)


class CheckoutForm(StrictForm):
    attempt_key = forms.RegexField(regex=r'^[A-Za-z0-9_-]{32,128}$', min_length=32, max_length=128, widget=forms.HiddenInput)
    customer_name = forms.CharField(max_length=100, required=False, label='Fictional customer name')
    scenario = forms.ChoiceField(choices=SCENARIOS, label='Synthetic payment scenario')

    def clean_customer_name(self):
        value = unicodedata.normalize('NFC', self.cleaned_data['customer_name'].strip())
        if re.search(r'\b[0-9][0-9 -]{11,}[0-9]\b', value):
            raise ValidationError('Use a fictional name. Payment card and account details are not accepted.')
        return value


class ActionForm(StrictForm):
    action_key = forms.RegexField(regex=r'^[A-Za-z0-9_-]{32,128}$', min_length=32, max_length=128, widget=forms.HiddenInput)
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class CaptureForm(ActionForm):
    amount = forms.CharField(max_length=16, widget=forms.TextInput(attrs={'inputmode': 'decimal'}))

    def clean_amount(self):
        return parse_amount(self.cleaned_data['amount'], minimum=100)


class RefundForm(ActionForm):
    amount = forms.CharField(max_length=16, widget=forms.TextInput(attrs={'inputmode': 'decimal'}))
    reason = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={'rows': 3}))

    def clean_amount(self):
        return parse_amount(self.cleaned_data['amount'])
