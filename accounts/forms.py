from django import forms
from django.contrib.auth.password_validation import validate_password
from .domain import normalize_email
class StrictAccountForm(forms.Form):
    transport_fields = {'csrfmiddlewaretoken', 'next', 'begin', 'submit'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.unknown_fields = set(self.data) - set(self.fields) - self.transport_fields if self.is_bound else set()

    def clean(self):
        cleaned = super().clean()
        if self.unknown_fields:
            raise forms.ValidationError('This form contains unsupported fields. Reload the page and try again.')
        return cleaned


class RegistrationForm(StrictAccountForm):
    email=forms.EmailField(max_length=254)
    password=forms.CharField(min_length=15,max_length=128,strip=False,widget=forms.PasswordInput)
    merchant_name=forms.CharField(max_length=100,label='Fictional business name')
    timezone=forms.CharField(max_length=64,initial='UTC')
    def clean_email(self):return normalize_email(self.cleaned_data['email'])
    def clean_password(self):
        password=self.cleaned_data['password'];validate_password(password);return password
    def clean_merchant_name(self):
        import unicodedata
        return unicodedata.normalize('NFC',self.cleaned_data['merchant_name'])
    def clean_timezone(self):
        from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
        value=self.cleaned_data['timezone']
        try:ZoneInfo(value)
        except ZoneInfoNotFoundError:raise forms.ValidationError('Choose a valid IANA timezone, such as America/Detroit.')
        return value
class LoginForm(StrictAccountForm):
    email=forms.EmailField(max_length=254)
    password=forms.CharField(max_length=128,strip=False,widget=forms.PasswordInput)
class CodeForm(StrictAccountForm):
    code=forms.CharField(max_length=40,label='Authenticator or recovery code',widget=forms.TextInput(attrs={'autocomplete':'one-time-code'}))
class StepUpForm(StrictAccountForm):
    password=forms.CharField(max_length=128,strip=False,widget=forms.PasswordInput)
    code=forms.CharField(max_length=6,label='Fresh authenticator code')
class ResetForm(StrictAccountForm):
    email=forms.EmailField(max_length=254)
class PasswordForm(StrictAccountForm):
    password=forms.CharField(min_length=15,max_length=128,strip=False,widget=forms.PasswordInput)
    def clean_password(self):
        value=self.cleaned_data['password'];validate_password(value);return value
