from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LogoutView
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from modem.exceptions import ModemError, ModemRateLimited, ModemSessionExpired
from modem.forms import ModemLoginForm
from modem.models import ModemConnection
from modem.services import browser_owner, get_modem_service


@login_required
@never_cache
@require_GET
def dashboard(request):
    authenticated = get_modem_service().is_authenticated(browser_owner(request))
    return render(
        request,
        "modem/dashboard.html",
        {
            "authenticated": authenticated,
            "monitor": get_modem_service().monitor.snapshot()
            if settings.MODEM_MONITOR_ENABLED
            else None,
            "modem_url": settings.MODEM_BASE_URL,
            "connection": ModemConnection.objects.filter(name="default").first(),
        },
    )


@sensitive_post_parameters("password")
@sensitive_variables()
@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def login_view(request):
    form = ModemLoginForm(request.POST if request.method == "POST" else None)
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            get_modem_service().login(browser_owner(request), **form.cleaned_data)
        except ModemRateLimited:
            form.add_error(None, "Too many login attempts. Try again in five minutes.")
            status = 429
        except ModemError:
            form.add_error(None, "Unable to authenticate with the modem.")
            status = 400
        else:
            messages.success(request, "Modem login successful.")
            return redirect("modem:dashboard")
    return render(request, "modem/login.html", {"form": form}, status=status)


@login_required
@require_POST
def reconnect(request):
    try:
        get_modem_service().reconnect_network(browser_owner(request))
    except ModemSessionExpired:
        messages.error(
            request,
            "Modem authentication has expired or could not be verified. Log in again.",
        )
        return redirect("modem:login")
    except ModemRateLimited:
        messages.warning(request, "Please wait 30 seconds before reconnecting again.")
    except ModemError:
        messages.error(
            request,
            "Unable to confirm reconnect. The modem may have acted. Log in again before another attempt.",
        )
        return redirect("modem:login")
    else:
        messages.success(request, "Network reconnect requested successfully.")
    return redirect("modem:dashboard")


@login_required
@require_POST
def logout_view(request):
    get_modem_service().logout(browser_owner(request))
    messages.success(
        request,
        "Monitoring retains the modem session."
        if settings.MODEM_MONITOR_ENABLED
        else "Modem session cleared.",
    )
    return redirect("modem:login")


class AppLogoutView(LogoutView):
    def post(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            get_modem_service().logout(browser_owner(request))
        return super().post(request, *args, **kwargs)


@require_GET
def health(request):
    return JsonResponse({"status": "ok"})
