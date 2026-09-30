from django import forms


class ModemLoginForm(forms.Form):
    username = forms.CharField(
        max_length=128, widget=forms.TextInput(attrs={"autocomplete": "off"})
    )
    password = forms.CharField(
        max_length=1024,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
    )

    def clean_password(self):
        value = self.cleaned_data["password"]
        if not value.strip():
            raise forms.ValidationError("Enter a password.")
        return value
