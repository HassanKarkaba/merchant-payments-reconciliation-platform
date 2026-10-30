from django import forms
class ResponseForm(forms.Form):
    text=forms.CharField(min_length=1,max_length=2000,widget=forms.Textarea,label='Plain-text dispute response',help_text='Use fictional evidence only. Acceptance stores the response and does not guarantee a win.')
    expected_version=forms.IntegerField(min_value=1,widget=forms.HiddenInput)
    action_key=forms.CharField(min_length=32,max_length=128,widget=forms.HiddenInput)

    def clean(self):
        values=super().clean()
        if set(self.data)-set(self.fields)-{'csrfmiddlewaretoken'}:raise forms.ValidationError('Unsupported response fields. Refresh and try again.')
        return values
