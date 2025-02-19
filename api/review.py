import io
import logging
from flask import current_app
from users import (
    mark_user_for_manual_review as _mark_user_for_manual_review,
    user_reviewed as _user_reviewed,
    rollback_reviewed as _rollback_reviewed,
    get_user as _get_user,
    allocate_review_user as _allocate_review_user,
    rollback_manual_review as _rollback_manual_review,
    pop_possible_duplicate_with as _pop_possible_duplicate_with,
    add_possible_duplicate_with as _add_possible_duplicate_with,
    get_face_metadata_pending_review as _get_face_metadata_pending_review
)
from minio_uploader import (
    put_review_photo as _put_review_photo,
    get_review_photo as _get_review_photo,
    get_primary_photo as _get_primary_photo,
    delete_review_photo as _delete_review_photo
)
from webhook import callback, UnauthorizedFromWebhook
import primary_photo
from auth import Token
import exceptions
import metrics
from faces import _faces_count_to_search_for, find_similar_users
class UserForReview:
    def __init__(self, user, primary_photo, possible_duplicates):
        self.user_id = user.get("user_id")
        self.ip = user.get("ip")
        self.retries = user.get("duplicate_review_count", 0)
        self.primary_photo = primary_photo
        self.possible_duplicates = possible_duplicates

class Duplicate:
    def __init__(self, user_id, primary_photo):
        self.user_id = user_id
        self.primary_photo = primary_photo

def primary_photo_to_review(now, current_user, user_id, user, photo_stream,metadata, similar_users, ip, e):
    logging.info(f"Face {user_id}  is matching with user {similar_users[0]}, user is forwarded to manual review: distance {e.sface_distance} < {current_app.config['PRIMARY_PHOTO_SFACE_DISTANCE']}, {e.arface_distance} < {current_app.config['PRIMARY_PHOTO_ARCFACE_DISTANCE']}")
    _mark_user_for_manual_review(user_id,ip,similar_users,user.get("duplicate_review_count",0), metadata)
    _put_review_photo(user_id,photo_stream)
    metrics.primary_photo_to_review()
    try:
        callback(
            current_user=current_user,
            primary_md={"uploaded_at": now},
            secondary_md=None,
            user=user,
            potentially_duplicate=True
        )
    except UnauthorizedFromWebhook as e:
        _rollback_manual_review(user_id)
        raise e
    except Exception as e:
        _rollback_manual_review(user_id)
        raise e
    raise exceptions.UserForwardedToManualReview(f"user {user_id} forwarded to manual review: {str(e)}")

def make_decision(now: int, admin_current_user: Token, user_id:str, decision: str, most_similar_user_to_duplicate: str = None):
    user = _get_user(user_id)
    if not user:
        raise exceptions.UserNotFound("user have no state")
    if not user.get("possible_duplicate_with",[]):
        # possible_duplicate_with might be gone when most similar users are blocked
        if user.get("duplicate_review_count", None) is None or (not user.get("ip","")):
            raise exceptions.NoDataException(f"user {user_id} is not on review")
    if most_similar_user_to_duplicate:
        if most_similar_user_to_duplicate in user.get("possible_duplicate_with",[]):
            if decision == "duplicate":
                logging.warning(f"admin decision - most similar user {most_similar_user_to_duplicate} of user_id {user_id} = {decision} processing by admin {admin_current_user.user_id}")
                try:
                    photo = _get_primary_photo(most_similar_user_to_duplicate)
                    try:
                        primary_photo.primary_photo_declined(exceptions.DisableByAdmin("manually disabled by admin", -1, -1, [user_id]), now, admin_current_user, most_similar_user_to_duplicate, io.BytesIO(photo), admin_perm_user_id=most_similar_user_to_duplicate)
                    except exceptions.UserDisabled as e:
                        pass
                    return _pop_possible_duplicate_with(user_id,user,most_similar_user_to_duplicate)
                except Exception as e:
                    _pop_possible_duplicate_with(user_id,user,most_similar_user_to_duplicate)
                    raise e
            else:
                raise Exception("most similar users are only allowed with decision=duplicate")
        else:
            raise exceptions.NoDataException(f"user {most_similar_user_to_duplicate} is not on review")
    photo = _get_review_photo(user_id)
    if not photo:
        raise exceptions.UserNotFound("user have no photo sent to review")
    logging.warning(f"admin decision - user_id {user_id} = {decision} processing by admin {admin_current_user.user_id}")
    if decision == "duplicate":
        _user_reviewed(admin_id=admin_current_user.user_id,user_id=user_id,retry=False)
        try:
            primary_photo.primary_photo_declined(exceptions.DisableByAdmin("manually disabled by admin", -1, -1, user.get("possible_duplicate_with",[])), now, admin_current_user, user_id, io.BytesIO(photo), admin_perm_user_id=user_id)
            _delete_review_photo(user_id)
        except exceptions.UserDisabled as e:
            pass
        except Exception as e:
            _rollback_reviewed(admin_id=admin_current_user.user_id,user_id=user_id,user = user,retry=False)
            raise e
    elif decision == "retry":
        _user_reviewed(admin_id=admin_current_user.user_id,user_id=user_id,retry=True)
        primary_photo.delete_user_photos_and_metadata(current_user=admin_current_user,to_delete_user_id=user_id, keep_retries=True)
    elif decision == "not_duplicate":
        try:
            sface_md = None
            md = _get_face_metadata_pending_review(user_id)
            if not md:
                _, md, sface_md = primary_photo.extract_metadatas(user_id,io.BytesIO(photo))
            if sface_md is None:
                _,_, sface_md = primary_photo.extract_metadatas(user_id,io.BytesIO(photo),calc_arcface=False)
            md = [float(x) for x in md]
            _user_reviewed(admin_id=admin_current_user.user_id,user_id=user_id,retry=False)
            primary_photo.primary_photo_passed(now, admin_current_user, user_id,user, io.BytesIO(photo), sface_md, md, -1, admin_perm_user_id=user_id)
            _delete_review_photo(user_id)
        except Exception as e:
            _rollback_reviewed(admin_id=admin_current_user.user_id,user_id=user_id,user=user,retry=False)
            raise e
    else:
        raise Exception(f"invalid decision:{decision}")
    logging.warning(f"admin decision - user_id {user_id} = {decision} processed by admin {admin_current_user.user_id}")


def next_user_for_review(admin_id):
    user_id, review_queue_len = _allocate_review_user(admin_id)
    d = []
    if user_id:
        user = _get_user(user_id)
        selfie = _get_review_photo(user_id)
        if not user:
            user = {"user_id": user_id}
        if not selfie:
            primary = _get_primary_photo(user_id)
            if primary:
                _user_reviewed(admin_id,user_id, False)
                return next_user_for_review(admin_id)
        duplicates = [d for id in user.get("possible_duplicate_with",[]) if (d:= fetch_duplicate(id)) is not None]
        if len(duplicates) == 0:
            threshold = current_app.config['PRIMARY_PHOTO_ARCFACE_DISTANCE']
            md = _get_face_metadata_pending_review(user_id)
            if not md:
                _, md, sface_md = primary_photo.extract_metadatas(user_id,io.BytesIO(selfie))
            md = [float(x) for x in md]
            extra_users, extra_distances = find_similar_users(user_id, md, threshold)
            if user_id in extra_users: extra_users.remove(user_id)
            duplicates.extend([d for id in extra_users if (d:= fetch_duplicate(id)) is not None and not id in user.get("possible_duplicate_with",[])])
            _add_possible_duplicate_with(user_id, user, extra_users)
        return UserForReview(user,primary_photo=selfie, possible_duplicates = duplicates ), review_queue_len
    return None, 0

def fetch_duplicate(user_id):
    photo = _get_primary_photo(user_id)
    if photo is None:
        return None
    user = _get_user(user_id)
    if user is not None and user.get("disabled_at",0) > 0:
        return None
    return Duplicate(user_id, photo)