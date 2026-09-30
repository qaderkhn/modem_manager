from django.urls import path

from . import views

app_name = "modem"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("login/", views.login_view, name="login"),
    path("reconnect/", views.reconnect, name="reconnect"),
    path("logout/", views.logout_view, name="logout"),
]
