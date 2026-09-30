# In-memory modem sessions require exactly one process and one replica.
bind = "127.0.0.1:8000"
workers = 1
worker_class = "gthread"
threads = 4
timeout = 120
accesslog = None
errorlog = "-"
preload_app = False


def post_worker_init(worker):
    from django.conf import settings

    from modem.monitoring import MonitorRunner
    from modem.services import get_modem_service

    if settings.MODEM_MONITOR_ENABLED:
        worker.lte_monitor = MonitorRunner(get_modem_service())
        worker.lte_monitor.start()


def worker_exit(server, worker):
    runner = getattr(worker, "lte_monitor", None)
    if runner:
        runner.stop()


def on_starting(server):
    if server.cfg.workers != 1 or server.cfg.preload_app:
        raise RuntimeError("Modem Manager requires one worker without preload")


def pre_fork(server, worker):
    # A reload can overlap workers even with workers=1. Reject overlap before any
    # device client is initialized. Use stop/start for deployments, never HUP/USR2.
    if server.WORKERS:
        raise RuntimeError("Overlapping modem workers are forbidden; stop then start")
