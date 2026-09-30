from django.contrib import admin

from modem.models import ModemConnection


@admin.register(ModemConnection)
class ModemConnectionAdmin(admin.ModelAdmin):
    list_display = ("name", "base_url", "last_login_at", "last_reconnect_at")
    readonly_fields = (
        "name",
        "base_url",
        "last_login_at",
        "last_reconnect_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
