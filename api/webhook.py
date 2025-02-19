from flask import current_app
import requests, backoff
import logging
from datetime import datetime

def _check_status_code(r):
    if r is None or r.response is None:
        return False

    return r.response.status_code == 401 or r.response.status_code == 404

def _log(r):
    e = r["exception"]

    if e is None or e.response is None:
        status_code = ''
        response_text = ''
    else:
        status_code = e.response.status_code
        response_text = e.response.text

    logging.error(f"Call to {current_app.config['METADATA_UPDATED_CALLBACK_URL']} failed with {str(e)} {status_code} {response_text}, retrying...")

def _log_migrate_phone_login(r):
    e = r["exception"]

    if e is None or e.response is None:
        status_code = ''
        response_text = ''
    else:
        status_code = e.response.status_code
        response_text = e.response.text

    logging.error(f"Call to {current_app.config['MIGRATE_PHONE_LOGIN_CALLBACK_URL']} failed with {str(e)} {status_code} {response_text}, retrying...")

@backoff.on_exception(backoff.constant,
                      (requests.exceptions.Timeout,
                       requests.exceptions.ConnectionError,
                       requests.HTTPError),
                      giveup=_check_status_code,
                      on_backoff=_log,
                      interval = 0.1,
                      max_tries = 15,
                      max_time = 25
                      )
def callback(current_user, primary_md, secondary_md, user, user_id = "", potentially_duplicate = False):
    time_format = '%Y-%m-%dT%H:%M:%S.%fZ%Z'
    url = current_app.config['METADATA_UPDATED_CALLBACK_URL']
    if not url:
        return

    disabled = False
    if user is not None:
        disabled = user["disabled_at"] is not None and user["disabled_at"] > 0
    lastUpdated = []
    if primary_md is not None:
        lastUpdated = [datetime.utcfromtimestamp(primary_md["uploaded_at"]/1e9).strftime(time_format)]
    if secondary_md is not None:
        lastUpdated.append(datetime.utcfromtimestamp(secondary_md["uploaded_at"]/1e9).strftime(time_format))
    processing_user_id = user_id if user_id else current_user.user_id
    webhook_result = requests.post(url=url+"?userId="+processing_user_id, headers={
        "Authorization": f"Bearer {current_user.raw_token}",
        "X-Account-Metadata": current_user.metadata,
        "X-API-Key": current_app.config["METADATA_UPDATED_SECRET"],
        "X-User-ID": user_id
    }, json={"lastUpdatedAt":lastUpdated, "disabled": disabled, "potentiallyDuplicate": potentially_duplicate}, verify=False, timeout=25)

    try:
        webhook_result.raise_for_status()
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            raise UnauthorizedFromWebhook(e.response.text)
        else:
            raise e

class UnauthorizedFromWebhook(Exception):
    def __init__(self, message):
        super().__init__(message)

class MigratePhoneLoginWebhookBadRequest(Exception):
    def __init__(self, message):
        super().__init__(message)

class MigratePhoneLoginWebhookConflict(Exception):
    def __init__(self, message):
        super().__init__(message)

class MigratePhoneLoginWebhookRateLimit(Exception):
    def __init__(self, message):
        super().__init__(message)

@backoff.on_exception(backoff.constant,
                      (requests.exceptions.Timeout,
                       requests.exceptions.ConnectionError,
                       requests.HTTPError),
                      on_backoff=_log_migrate_phone_login,
                      interval = 0.1,
                      max_tries = 15,
                      max_time = 49
                      )
def callback_migrate_phone_login(current_user, user_id):
    url = current_app.config['MIGRATE_PHONE_LOGIN_CALLBACK_URL']
    if not url:
        return

    webhook_result = requests.post(url=url+"?userId="+user_id, headers={
        "X-API-Key": current_app.config["METADATA_UPDATED_SECRET"],
        "X-User-ID": user_id
    }, json={"email": current_user.email, "deviceUniqueId": current_user.device_unique_id, "language": current_user.language}, verify=False, timeout=25)

    try:
        res = webhook_result.json()
        webhook_result.raise_for_status()

        return res['loginSession']
    except requests.HTTPError as e:
        if e.response.status_code == 400:
            raise MigratePhoneLoginWebhookBadRequest(e.response.text)
        if e.response.status_code == 409:
            raise MigratePhoneLoginWebhookConflict(e.response.text)
        if e.response.status_code == 429:
            raise MigratePhoneLoginWebhookRateLimit(e.response.text)
        else:
            raise e
