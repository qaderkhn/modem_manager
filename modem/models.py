from django.db import models


class ModemConnection(models.Model):
    name = models.CharField(max_length=100, unique=True)
    base_url = models.URLField()
    last_login_at = models.DateTimeField(null=True, blank=True)
    last_reconnect_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name
