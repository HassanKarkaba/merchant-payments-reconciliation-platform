import unicodedata
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from django import forms
from accounts.domain import normalize_email
class MerchantForm(forms.Form):
    name=forms.CharField(max_length=100,label='Fictional business name')
    timezone=forms.CharField(max_length=64)
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    def clean_name(self):return unicodedata.normalize('NFC',self.cleaned_data['name'])
    def clean_timezone(self):
        value=self.cleaned_data['timezone']
        try:ZoneInfo(value)
        except ZoneInfoNotFoundError:raise forms.ValidationError('Use a valid IANA timezone.')
        return value
class InviteForm(forms.Form):
    email=forms.EmailField(max_length=254)
    role=forms.ChoiceField(choices=[('OWNER','Owner'),('FINANCE','Finance operator'),('ANALYST','Analyst')])
    def clean_email(self):return normalize_email(self.cleaned_data['email'])
