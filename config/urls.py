from django.contrib import admin
from django.contrib.auth.views import LoginView
from django.urls import include, path
from django.views.generic import RedirectView

from modem.views import AppLogoutView, health

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="modem:dashboard"), name="home"),
    path(
        "accounts/login/",
        LoginView.as_view(template_name="registration/login.html"),
        name="app_login",
    ),
    path("accounts/logout/", AppLogoutView.as_view(), name="app_logout"),
    path("admin/", admin.site.urls),
    path("modem/", include("modem.urls")),
    path("health/", health, name="health"),
]
