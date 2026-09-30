"""Read mounted deployment secrets only when authentication is needed."""

from pathlib import Path

from django.views.decorators.debug import sensitive_variables

from modem.exceptions import ModemAuthenticationError


class FileCredentialProvider:
    def __init__(self, username_file, password_file):
        self.username_file = username_file
        self.password_file = password_file

    @sensitive_variables()
    def load(self):
        try:
            values = []
            for path in (self.username_file, self.password_file):
                if not path:
                    raise ValueError
                with Path(path).open("rb") as source:
                    value = source.read(4097)
                if len(value) > 4096:
                    raise ValueError
                value = value.decode("utf-8").removesuffix("\n").removesuffix("\r")
                if not value.strip() or "\n" in value or "\r" in value or "\0" in value:
                    raise ValueError
                values.append(value)
            return tuple(values)
        except (OSError, ValueError, UnicodeError):
            raise ModemAuthenticationError("Deployment credentials are unavailable") from None
