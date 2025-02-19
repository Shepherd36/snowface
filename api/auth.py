from functools import wraps
import jwt, json
from flask import request, abort
from flask import current_app
from flask import g
from firebase_admin import auth, initialize_app, credentials
import logging


_issuer_ice = "ice.io/access"
_issuer_ice_metadata = "ice.io/metadata"
_registered_with = "registeredWithProvider"
_ice_id = "iceId"
_firebase_id = "firebaseId"

_firebase_client = None
class Token:
    user_id: str
    email: str
    role: str
    raw_token: str
    metadata: str
    language: str
    device_unique_id: str
    phone_number_migration: bool
    send_email_magic_link: bool
    _provider: str

    def __init__(self, token, user_id: str, email: str, role: str, provider: str, device_unique_id = "", language = "", phone_number_migration = False, send_email_magic_link = False):
        self.raw_token = token
        self.user_id = user_id
        self.email = email
        self.role = role
        self._provider = provider
        self.raw_token = token
        self.metadata = ""
        self.device_unique_id = device_unique_id
        self.language = language
        self.phone_number_migration = phone_number_migration
        self.send_email_magic_link = send_email_magic_link

    def isICE(self):
        return self._provider == "ice"

def wrapped_auth_required(allow_migrate_phone_number_to_email = False):
    def auth_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            token = None
            if allow_migrate_phone_number_to_email and "X-Migrate-Phone-Number-To-Email" in request.headers:
                language = ""
                device_unique_id = ""
                email = ""
                user_id = request.view_args.get("user_id","")
                send_email_magic_link = False

                if "X-Migrate-Phone-Number-Language" in request.headers:
                    language = request.headers["X-Migrate-Phone-Number-Language"]
                if "X-Migrate-Phone-Number-Device-Unique-Id" in request.headers:
                    device_unique_id = request.headers["X-Migrate-Phone-Number-Device-Unique-Id"]
                if "X-Migrate-Phone-Number-Email" in request.headers:
                    email = request.headers["X-Migrate-Phone-Number-Email"]
                if request.headers and "X-Send-Email-Magic-Link" in request.headers:
                    send_email_magic_link = True

                user = Token(
                    token=None,
                    user_id=user_id,
                    email=email,
                    role='',
                    provider='',
                    device_unique_id=device_unique_id,
                    language=language,
                    phone_number_migration=True,
                    send_email_magic_link=send_email_magic_link
                )

                return f(user, *args, **kwargs)

            if "Authorization" in request.headers:
                token = request.headers["Authorization"].replace("Bearer ","",1)

            if not token:
                return {
                    "message": "Authorization token not presented",
                    "code": "INVALID_TOKEN",
                    "error": "Unauthorized"
                }, 401
            try:
                data = jwt.decode(token, options={"verify_signature": False})
                if data["iss"] == _issuer_ice:
                    user = _parse_ice(token)
                else:
                    user = _parse_firebase(token)

                if "X-Account-Metadata" in request.headers:
                    user = _modify_with_metadata(user, request.headers["X-Account-Metadata"])

                user_id_in_url = request.view_args.get("user_id","")
                if user_id_in_url and user_id_in_url != user.user_id:
                    logging.error(f"operation not allowed. uri>{user_id_in_url}!=token>{user.user_id}")

                    return {
                        "message": f"operation not allowed. uri>{user_id_in_url}!=token>{user.user_id}",
                        "code": "OPERATION_NOT_ALLOWED"
                    }, 403

            except Exception as e:
                logging.error(e)

                return {
                    "message": str(e),
                    "code": "INVALID_TOKEN",
                }, 401

            return f(user, *args, **kwargs)

        return decorated
    return auth_required

def _parse_ice(token):
    jwt_data=jwt.decode(token, current_app.config["JWT_SECRET"], algorithms=["HS256"])

    return Token(
        token,
        jwt_data["sub"],
        jwt_data["email"],
        jwt_data["role"],
        "ice"
    )

def _parse_firebase(token):
    jwt_data = auth.verify_id_token(token, app = _get_firebase_client())

    return Token(
        token,
        jwt_data["uid"],
        jwt_data.get("email",""),
        jwt_data.get("role",""),
        "firebase"
    )

def _get_firebase_client():
    global _firebase_client
    if _firebase_client is None:
       _firebase_client = initialize_app(credential=credentials.Certificate(json.loads(current_app.config['GOOGLE_APPLICATION_CREDENTIALS'])))

    return _firebase_client

def _modify_with_metadata(user, mdToken):
    if not mdToken:
        return user

    user_id = user.user_id
    metadata = jwt.decode(mdToken, current_app.config["JWT_SECRET"], algorithms=["HS256"])
    if metadata["iss"] != _issuer_ice_metadata:
        raise jwt.InvalidIssuerError(f'{metadata["iss"]} must be {_issuer_ice_metadata}')

    sub_match = metadata["sub"] != "" and user_id == metadata["sub"]
    fb_match = metadata.get(_firebase_id,"") != "" and user_id == metadata.get(_firebase_id,"")
    ice_match = metadata.get(_ice_id,"") != "" and user_id == metadata.get(_ice_id,"")
    if user_id and not (sub_match or fb_match or ice_match):
        raise jwt.InvalidTokenError(f"token {user_id} does not own metadata {metadata}")

    md_user_id = ""
    registeredWithProvider = metadata.get(_registered_with,"")
    if registeredWithProvider == "firebase":
        md_user_id = metadata.get(_firebase_id,"")
    elif registeredWithProvider == "ice":
        md_user_id = metadata.get(_ice_id,"")
    if md_user_id:
        user.user_id = md_user_id
        user.metadata = mdToken

    return user
