import logging

from prometheus_client import multiprocess
import time, os
import metrics
from service import emotions_cleanup, _default_session_duration, stop_wrongfully_disabled_users_worker
from apscheduler.schedulers.background import BackgroundScheduler
from users import clean_wrongfully_disabled_users_workers
def worker_exit(server, worker):
    multiprocess.mark_process_dead(worker.pid)
    stop_wrongfully_disabled_users_worker()
def pre_request(worker, request):
    if len(logging.root.handlers) == 0:
        logging.basicConfig(level=os.environ.get('LOGGING_LEVEL','INFO'), format="%(asctime)s.%(msecs)d %(levelname)s:%(name)s:PID:%(process)d %(message)s")
    q_header = [float(h[1]) for h in request.headers if h[0].lower() == "x-queued-time"]
    queued_time = None
    if len(q_header) > 0:
        queued_time = q_header[0]
    if queued_time:
        latency = time.time() - queued_time
        userIdIdx = -1
        if "/liveness/" in request.path:
            userIdIdx = -2
        metrics.register_gunicorn_latency("/".join(request.path.split("/")[:userIdIdx]), latency)

def on_starting(server):
    clean_wrongfully_disabled_users_workers()