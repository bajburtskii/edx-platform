"""
Discussion API internal interface
"""
from __future__ import annotations

import itertools
from collections import defaultdict
from typing import Dict, List, Literal, Optional, Set, Tuple
from urllib.parse import urlencode, urlunparse

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.http import Http404
from django.urls import reverse
from enum import Enum
from opaque_keys import InvalidKeyError
from opaque_keys.edx.locator import CourseKey
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request

from lms.djangoapps.courseware.courses import get_course_with_access
from lms.djangoapps.courseware.exceptions import CourseAccessRedirect
from openedx.core.djangoapps.discussions.utils import get_accessible_discussion_xblocks
from openedx.core.djangoapps.django_comment_common.comment_client.comment import Comment
from openedx.core.djangoapps.django_comment_common.comment_client.course import get_course_commentable_counts
from openedx.core.djangoapps.django_comment_common.comment_client.thread import Thread
from openedx.core.djangoapps.django_comment_common.comment_client.utils import CommentClientRequestError
from openedx.core.djangoapps.django_comment_common.models import (
    CourseDiscussionSettings,
    FORUM_ROLE_ADMINISTRATOR,
    FORUM_ROLE_COMMUNITY_TA,
    FORUM_ROLE_MODERATOR,
)
from openedx.core.djangoapps.django_comment_common.signals import (
    comment_created,
    comment_deleted,
    comment_edited,
    comment_voted,
    thread_created,
    thread_deleted,
    thread_edited,
    thread_voted,
)
from openedx.core.djangoapps.user_api.accounts.api import get_account_settings
from openedx.core.lib.exceptions import CourseNotFoundError, DiscussionNotFoundError, PageNotFoundError
from xmodule.course_module import CourseBlock
from xmodule.tabs import CourseTabList
from .exceptions import (
    CommentNotFoundError,
    DiscussionBlackOutException,
    DiscussionDisabledError,
    ThreadNotFoundError,
)
from .forms import CommentActionsForm, ThreadActionsForm
from .pagination import DiscussionAPIPagination
from .permissions import (
    can_delete,
    get_editable_fields,
    get_initializable_comment_fields,
    get_initializable_thread_fields,
)
from .serializers import (
    CommentSerializer,
    DiscussionTopicSerializer,
    ThreadSerializer,
    get_context,
)
from .utils import discussion_open_for_user
from ..django_comment_client.base.views import (
    track_comment_created_event,
    track_thread_created_event,
    track_voted_event,
)
from ..django_comment_client.utils import (
    get_group_id_for_user,
    get_user_role_names,
    is_commentable_divided,
)

User = get_user_model()

ThreadType = Literal["discussion", "question"]
ViewType = Literal["unread", "unanswered"]
ThreadOrderingType = Literal["last_activity_at", "comment_count", "vote_count"]


class DiscussionTopic:
    """
    Class for discussion topic structure
    """

    def __init__(
        self,
        topic_id: Optional[str],
        name: str,
        thread_list_url: str,
        children: Optional[List[DiscussionTopic]] = None,
        thread_counts: Dict[str, int] = None,
    ):
        self.id = topic_id  # pylint: disable=invalid-name
        self.name = name
        self.thread_list_url = thread_list_url
        self.children = children or []  # children are of same type i.e. DiscussionTopic
        if not children and not thread_counts:
            thread_counts = {"discussion": 0, "question": 0}
        self.thread_counts = thread_counts


class DiscussionEntity(Enum):
    """
    Enum for different types of discussion related entities
    """
    thread = 'thread'
    comment = 'comment'


def _get_course(course_key, user):
    """
    Get the course descriptor, raising CourseNotFoundError if the course is not found or
    the user cannot access forums for the course, and DiscussionDisabledError if the
    discussion tab is disabled for the course.
    """
    try:
        course = get_course_with_access(user, 'load', course_key, check_if_enrolled=True)
    except (Http404, CourseAccessRedirect) as err:
        # Convert 404s into CourseNotFoundErrors.
        # Raise course not found if the user cannot access the course
        raise CourseNotFoundError("Course not found.") from err

    discussion_tab = CourseTabList.get_tab_by_type(course.tabs, 'discussion')
    if not (discussion_tab and discussion_tab.is_enabled(course, user)):
        raise DiscussionDisabledError("Discussion is disabled for the course.")

    return course


def _get_thread_and_context(request, thread_id, retrieve_kwargs=None):
    """
    Retrieve the given thread and build a serializer context for it, returning
    both. This function also enforces access control for the thread (checking
    both the user's access to the course and to the thread's cohort if
    applicable). Raises ThreadNotFoundError if the thread does not exist or the
    user cannot access it.
    """
    retrieve_kwargs = retrieve_kwargs or {}
    try:
        if "with_responses" not in retrieve_kwargs:
            retrieve_kwargs["with_responses"] = False
        if "mark_as_read" not in retrieve_kwargs:
            retrieve_kwargs["mark_as_read"] = False
        cc_thread = Thread(id=thread_id).retrieve(**retrieve_kwargs)
        course_key = CourseKey.from_string(cc_thread["course_id"])
        course = _get_course(course_key, request.user)
        context = get_context(course, request, cc_thread)
        course_discussion_settings = CourseDiscussionSettings.get(course_key)
        if (
                not context["is_requester_privileged"] and
                cc_thread["group_id"] and
                is_commentable_divided(course.id, cc_thread["commentable_id"], course_discussion_settings)
        ):
            requester_group_id = get_group_id_for_user(request.user, course_discussion_settings)
            if requester_group_id is not None and cc_thread["group_id"] != requester_group_id:
                raise ThreadNotFoundError("Thread not found.")
        return cc_thread, context
    except CommentClientRequestError as err:
        # params are validated at a higher level, so the only possible request
        # error is if the thread doesn't exist
        raise ThreadNotFoundError("Thread not found.") from err


def _get_comment_and_context(request, comment_id):
    """
    Retrieve the given comment and build a serializer context for it, returning
    both. This function also enforces access control for the comment (checking
    both the user's access to the course and to the comment's thread's cohort if
    applicable). Raises CommentNotFoundError if the comment does not exist or the
    user cannot access it.
    """
    try:
        cc_comment = Comment(id=comment_id).retrieve()
        _, context = _get_thread_and_context(request, cc_comment["thread_id"])
        return cc_comment, context
    except CommentClientRequestError as err:
        raise CommentNotFoundError("Comment not found.") from err


def _is_user_author_or_privileged(cc_content, context):
    """
    Check if the user is the author of a content object or a privileged user.

    Returns:
        Boolean
    """
    return (
        context["is_requester_privileged"] or
        context["cc_requester"]["id"] == cc_content["user_id"]
    )


def get_thread_list_url(request, course_key, topic_id_list=None, following=False):
    """
    Returns the URL for the thread_list_url field, given a list of topic_ids
    """
    path = reverse("thread-list")
    query_list = (
        [("course_id", str(course_key))] +
        [("topic_id", topic_id) for topic_id in topic_id_list or []] +
        ([("following", following)] if following else [])
    )
    return request.build_absolute_uri(urlunparse(("", "", path, "", urlencode(query_list), "")))


def get_course(request, course_key):
    """
    Return general discussion information for the course.

    Parameters:

        request: The django request object used for build_absolute_uri and
          determining the requesting user.

        course_key: The key of the course to get information for

    Returns:

        The course information; see discussion.rest_api.views.CourseView for more
        detail.

    Raises:

        CourseNotFoundError: if the course does not exist or is not accessible
        to the requesting user
    """
    def _format_datetime(dt):
        """
        Provide backwards compatible datetime formatting.

        Technically, both "2020-10-20T23:59:00Z" and "2020-10-20T23:59:00+00:00"
        are ISO-8601 compliant, though the latter is preferred. We've always
        just passed back whatever datetime.isoformat() generated for the
        blackout dates in the get_course function (the "+00:00" format). At some
        point, this broke the expectation of the mobile app code, which expects
        these dates to be formatted in the same way that DRF formats the other
        datetimes in this API (the "Z" format).

        For the sake of compatibility, we're doing a manual substitution back to
        the old format here. This is done with a replacement because it's
        possible (though really not recommended) to enter blackout dates in
        something other than the UTC timezone, in which case we should not do
        the substitution... though really, that would probably break mobile
        client parsing of the dates as well. :-P
        """
        return dt.isoformat().replace('+00:00', 'Z')

    course = _get_course(course_key, request.user)
    user_roles = get_user_role_names(request.user, course_key)
    return {
        "id": str(course_key),
        "blackouts": [
            {
                "start": _format_datetime(blackout["start"]),
                "end": _format_datetime(blackout["end"]),
            }
            for blackout in course.get_discussion_blackout_datetimes()
        ],
        "thread_list_url": get_thread_list_url(request, course_key),
        "following_thread_list_url": get_thread_list_url(request, course_key, following=True),
        "topics_url": request.build_absolute_uri(
            reverse("course_topics", kwargs={"course_id": course_key})
        ),
        "allow_anonymous": course.allow_anonymous,
        "allow_anonymous_to_peers": course.allow_anonymous_to_peers,
        "user_roles": user_roles,
        "user_is_privileged": bool(user_roles & {
            FORUM_ROLE_ADMINISTRATOR,
            FORUM_ROLE_MODERATOR,
            FORUM_ROLE_COMMUNITY_TA,
        })
    }


def get_courseware_topics(
    request: Request,
    course_key: CourseKey,
    course: CourseBlock,
    topic_ids: Optional[List[str]],
    thread_counts: Dict[str, Dict[str, int]],
) -> Tuple[List[Dict], Set[str]]:
    """
    Returns a list of topic trees for courseware-linked topics.

    Parameters:

        request: The django request objects used for build_absolute_uri.
        course_key: The key of the course to get discussion threads for.
        course: The course for which topics are requested.
        topic_ids: A list of topic IDs for which details are requested.
            This is optional. If None then all course topics are returned.
        thread_counts: A map of the thread ids to the count of each type of thread in them
           e.g. discussion, question

    Returns:
        A list of courseware topics and a set of existing topics among
        topic_ids.

    """
    courseware_topics = []
    existing_topic_ids = set()

    def get_xblock_sort_key(xblock):
        """
        Get the sort key for the xblock (falling back to the discussion_target
        setting if absent)
        """
        return xblock.sort_key or xblock.discussion_target

    def get_sorted_xblocks(category):
        """Returns key sorted xblocks by category"""
        return sorted(xblocks_by_category[category], key=get_xblock_sort_key)

    discussion_xblocks = get_accessible_discussion_xblocks(course, request.user)
    xblocks_by_category = defaultdict(list)
    for xblock in discussion_xblocks:
        xblocks_by_category[xblock.discussion_category].append(xblock)

    for category in sorted(xblocks_by_category.keys()):
        children = []
        for xblock in get_sorted_xblocks(category):
            if not topic_ids or xblock.discussion_id in topic_ids:
                discussion_topic = DiscussionTopic(
                    xblock.discussion_id,
                    xblock.discussion_target,
                    get_thread_list_url(request, course_key, [xblock.discussion_id]),
                    None,
                    thread_counts.get(xblock.discussion_id),
                )
                children.append(discussion_topic)

                if topic_ids and xblock.discussion_id in topic_ids:
                    existing_topic_ids.add(xblock.discussion_id)

        if not topic_ids or children:
            discussion_topic = DiscussionTopic(
                None,
                category,
                get_thread_list_url(request, course_key, [item.discussion_id for item in get_sorted_xblocks(category)]),
                children,
                None,
            )
            courseware_topics.append(DiscussionTopicSerializer(discussion_topic).data)

    return courseware_topics, existing_topic_ids


def get_non_courseware_topics(
    request: Request,
    course_key: CourseKey,
    course: CourseBlock,
    topic_ids: Optional[List[str]],
    thread_counts: Dict[str, Dict[str, int]]
) -> Tuple[List[Dict], Set[str]]:
    """
    Returns a list of topic trees that are not linked to courseware.

    Parameters:

        request: The django request objects used for build_absolute_uri.
        course_key: The key of the course to get discussion threads for.
        course: The course for which topics are requested.
        topic_ids: A list of topic IDs for which details are requested.
            This is optional. If None then all course topics are returned.
        thread_counts: A map of the thread ids to the count of each type of thread in them
           e.g. discussion, question

    Returns:
        A list of non-courseware topics and a set of existing topics among
        topic_ids.

    """
    non_courseware_topics = []
    existing_topic_ids = set()
    sorted_topics = sorted(list(course.discussion_topics.items()), key=lambda item: item[1].get("sort_key", item[0]))
    for name, entry in sorted_topics:
        if not topic_ids or entry['id'] in topic_ids:
            discussion_topic = DiscussionTopic(
                entry["id"], name, get_thread_list_url(request, course_key, [entry["id"]]),
                None,
                thread_counts.get(entry["id"])
            )
            non_courseware_topics.append(DiscussionTopicSerializer(discussion_topic).data)

            if topic_ids and entry["id"] in topic_ids:
                existing_topic_ids.add(entry["id"])

    return non_courseware_topics, existing_topic_ids


def get_course_topics(request: Request, course_key: CourseKey, topic_ids: Optional[Set[str]] = None):
    """
    Returns the course topic listing for the given course and user; filtered
    by 'topic_ids' list if given.

    Parameters:

        course_key: The key of the course to get topics for
        user: The requesting user, for access control
        topic_ids: A list of topic IDs for which topic details are requested

    Returns:

        A course topic listing dictionary; see discussion.rest_api.views.CourseTopicViews
        for more detail.

    Raises:
        DiscussionNotFoundError: If topic/s not found for given topic_ids.
    """
    course = _get_course(course_key, request.user)
    thread_counts = get_course_commentable_counts(course.id)

    courseware_topics, existing_courseware_topic_ids = get_courseware_topics(
        request, course_key, course, topic_ids, thread_counts
    )
    non_courseware_topics, existing_non_courseware_topic_ids = get_non_courseware_topics(
        request, course_key, course, topic_ids, thread_counts,
    )

    if topic_ids:
        not_found_topic_ids = topic_ids - (existing_courseware_topic_ids | existing_non_courseware_topic_ids)
        if not_found_topic_ids:
            raise DiscussionNotFoundError(
                "Discussion not found for '{}'.".format(", ".join(str(id) for id in not_found_topic_ids))
            )

    return {
        "courseware_topics": courseware_topics,
        "non_courseware_topics": non_courseware_topics,
    }


def _get_user_profile_dict(request, usernames):
    """
    Gets user profile details for a list of usernames and creates a dictionary with
    profile details against username.

    Parameters:

        request: The django request object.
        usernames: A string of comma separated usernames.

    Returns:

        A dict with username as key and user profile details as value.
    """
    if usernames:
        username_list = usernames.split(",")
    else:
        username_list = []
    user_profile_details = get_account_settings(request, username_list)
    return {user['username']: user for user in user_profile_details}


def _user_profile(user_profile):
    """
    Returns the user profile object. For now, this just comprises the
    profile_image details.
    """
    return {
        'profile': {
            'image': user_profile['profile_image']
        }
    }


def _get_users(discussion_entity_type, discussion_entity, username_profile_dict):
    """
    Returns users with profile details for given discussion thread/comment.

    Parameters:

        discussion_entity_type: DiscussionEntity Enum value for Thread or Comment.
        discussion_entity: Serialized thread/comment.
        username_profile_dict: A dict with user profile details against username.

    Returns:

        A dict of users with username as key and user profile details as value.
    """
    users = {}
    if discussion_entity['author']:
        users[discussion_entity['author']] = _user_profile(username_profile_dict[discussion_entity['author']])

    if (
            discussion_entity_type == DiscussionEntity.comment
            and discussion_entity['endorsed']
            and discussion_entity['endorsed_by']
    ):
        users[discussion_entity['endorsed_by']] = _user_profile(username_profile_dict[discussion_entity['endorsed_by']])
    return users


def _add_additional_response_fields(
        request, serialized_discussion_entities, usernames, discussion_entity_type, include_profile_image
):
    """
    Adds additional data to serialized discussion thread/comment.

    Parameters:

        request: The django request object.
        serialized_discussion_entities: A list of serialized Thread/Comment.
        usernames: A list of usernames involved in threads/comments (e.g. as author or as comment endorser).
        discussion_entity_type: DiscussionEntity Enum value for Thread or Comment.
        include_profile_image: (boolean) True if requested_fields has 'profile_image' else False.

    Returns:

        A list of serialized discussion thread/comment with additional data if requested.
    """
    if include_profile_image:
        username_profile_dict = _get_user_profile_dict(request, usernames=','.join(usernames))
        for discussion_entity in serialized_discussion_entities:
            discussion_entity['users'] = _get_users(discussion_entity_type, discussion_entity, username_profile_dict)

    return serialized_discussion_entities


def _include_profile_image(requested_fields):
    """
    Returns True if requested_fields list has 'profile_image' entity else False
    """
    return requested_fields and 'profile_image' in requested_fields


def _serialize_discussion_entities(request, context, discussion_entities, requested_fields, discussion_entity_type):
    """
    It serializes Discussion Entity (Thread or Comment) and add additional data if requested.

    For a given list of Thread/Comment; it serializes and add additional information to the
    object as per requested_fields list (i.e. profile_image).

    Parameters:

        request: The django request object
        context: The context appropriate for use with the thread or comment
        discussion_entities: List of Thread or Comment objects
        requested_fields: Indicates which additional fields to return
            for each thread.
        discussion_entity_type: DiscussionEntity Enum value for Thread or Comment

    Returns:

        A list of serialized discussion entities
    """
    results = []
    usernames = []
    include_profile_image = _include_profile_image(requested_fields)
    for entity in discussion_entities:
        if discussion_entity_type == DiscussionEntity.thread:
            serialized_entity = ThreadSerializer(entity, context=context).data
        elif discussion_entity_type == DiscussionEntity.comment:
            serialized_entity = CommentSerializer(entity, context=context).data
        results.append(serialized_entity)

        if include_profile_image:
            if serialized_entity['author'] and serialized_entity['author'] not in usernames:
                usernames.append(serialized_entity['author'])
            if (
                    'endorsed' in serialized_entity and serialized_entity['endorsed'] and
                    'endorsed_by' in serialized_entity and
                    serialized_entity['endorsed_by'] and serialized_entity['endorsed_by'] not in usernames
            ):
                usernames.append(serialized_entity['endorsed_by'])

    results = _add_additional_response_fields(
        request, results, usernames, discussion_entity_type, include_profile_image
    )
    return results


def get_thread_list(
    request: Request,
    course_key: CourseKey,
    page: int,
    page_size: int,
    topic_id_list: List[str] = None,
    text_search: Optional[str] = None,
    following: Optional[bool] = False,
    author: Optional[str] = None,
    thread_type: Optional[ThreadType] = None,
    flagged: Optional[bool] = None,
    view: Optional[ViewType] = None,
    order_by: ThreadOrderingType = "last_activity_at",
    order_direction: Literal["desc"] = "desc",
    requested_fields: Optional[List[Literal["profile_image"]]] = None,
    count_flagged: bool = None,
):
    """
    Return the list of all discussion threads pertaining to the given course

    Parameters:

    request: The django request objects used for build_absolute_uri
    course_key: The key of the course to get discussion threads for
    page: The page number (1-indexed) to retrieve
    page_size: The number of threads to retrieve per page
    count_flagged: If true, fetch the count of flagged items in each thread
    topic_id_list: The list of topic_ids to get the discussion threads for
    text_search A text search query string to match
    following: If true, retrieve only threads the requester is following
    author: If provided, retrieve only threads by this author
    thread_type: filter for "discussion" or "question threads
    flagged: filter for only threads that are flagged
    view: filters for either "unread" or "unanswered" threads
    order_by: The key in which to sort the threads by. The only values are
        "last_activity_at", "comment_count", and "vote_count". The default is
        "last_activity_at".
    order_direction: The direction in which to sort the threads by. The default
        and only value is "desc". This will be removed in a future major
        version.
    requested_fields: Indicates which additional fields to return
        for each thread. (i.e. ['profile_image'])

    Note that topic_id_list, text_search, and following are mutually exclusive.

    Returns:

    A paginated result containing a list of threads; see
    discussion.rest_api.views.ThreadViewSet for more detail.

    Raises:

    PermissionDenied: If count_flagged is set but the user isn't privileged
    ValidationError: if an invalid value is passed for a field.
    ValueError: if more than one of the mutually exclusive parameters is
      provided
    CourseNotFoundError: if the requesting user does not have access to the requested course
    PageNotFoundError: if page requested is beyond the last
    """
    exclusive_param_count = sum(1 for param in [topic_id_list, text_search, following] if param)
    if exclusive_param_count > 1:  # pragma: no cover
        raise ValueError("More than one mutually exclusive param passed to get_thread_list")

    cc_map = {"last_activity_at": "activity", "comment_count": "comments", "vote_count": "votes"}
    if order_by not in cc_map:
        raise ValidationError({
            "order_by":
                [f"Invalid value. '{order_by}' must be 'last_activity_at', 'comment_count', or 'vote_count'"]
        })
    if order_direction != "desc":
        raise ValidationError({
            "order_direction": [f"Invalid value. '{order_direction}' must be 'desc'"]
        })

    course = _get_course(course_key, request.user)
    context = get_context(course, request)

    author_id = None
    if author:
        try:
            author_id = User.objects.get(username=author).id
        except User.DoesNotExist:
            # Raising an error for a missing user leaks the presence of a username,
            # so just return an empty response.
            return DiscussionAPIPagination(request, 0, 1).get_paginated_response({
                "results": [],
                "text_search_rewrite": None,
            })

    if count_flagged and not context["is_requester_privileged"]:
        raise PermissionDenied("`count_flagged` can only be set by users with moderator access or higher.")

    query_params = {
        "user_id": str(request.user.id),
        "group_id": (
            None if context["is_requester_privileged"] else
            get_group_id_for_user(request.user, CourseDiscussionSettings.get(course.id))
        ),
        "page": page,
        "per_page": page_size,
        "text": text_search,
        "sort_key": cc_map.get(order_by),
        "author_id": author_id,
        "flagged": flagged,
        "thread_type": thread_type,
        "count_flagged": count_flagged,
    }

    if view:
        if view in ["unread", "unanswered"]:
            query_params[view] = "true"
        else:
            ValidationError({
                "view": [f"Invalid value. '{view}' must be 'unread' or 'unanswered'"]
            })

    if following:
        paginated_results = context["cc_requester"].subscribed_threads(query_params)
    else:
        query_params["course_id"] = str(course.id)
        query_params["commentable_ids"] = ",".join(topic_id_list) if topic_id_list else None
        query_params["text"] = text_search
        paginated_results = Thread.search(query_params)
    # The comments service returns the last page of results if the requested
    # page is beyond the last page, but we want be consistent with DRF's general
    # behavior and return a PageNotFoundError in that case
    if paginated_results.page != page:
        raise PageNotFoundError("Page not found (No results on this page).")

    results = _serialize_discussion_entities(
        request, context, paginated_results.collection, requested_fields, DiscussionEntity.thread
    )

    paginator = DiscussionAPIPagination(
        request,
        paginated_results.page,
        paginated_results.num_pages,
        paginated_results.thread_count
    )
    return paginator.get_paginated_response({
        "results": results,
        "text_search_rewrite": paginated_results.corrected_text,
    })


def get_comment_list(request, thread_id, endorsed, page, page_size, requested_fields=None):
    """
    Return the list of comments in the given thread.

    Arguments:

        request: The django request object used for build_absolute_uri and
          determining the requesting user.

        thread_id: The id of the thread to get comments for.

        endorsed: Boolean indicating whether to get endorsed or non-endorsed
          comments (or None for all comments). Must be None for a discussion
          thread and non-None for a question thread.

        page: The page number (1-indexed) to retrieve

        page_size: The number of comments to retrieve per page

        requested_fields: Indicates which additional fields to return for
        each comment. (i.e. ['profile_image'])

    Returns:

        A paginated result containing a list of comments; see
        discussion.rest_api.views.CommentViewSet for more detail.
    """
    response_skip = page_size * (page - 1)
    cc_thread, context = _get_thread_and_context(
        request,
        thread_id,
        retrieve_kwargs={
            "with_responses": True,
            "recursive": False,
            "user_id": request.user.id,
            "response_skip": response_skip,
            "response_limit": page_size,
        }
    )

    # Responses to discussion threads cannot be separated by endorsed, but
    # responses to question threads must be separated by endorsed due to the
    # existing comments service interface
    if cc_thread["thread_type"] == "question":
        if endorsed is None:  # lint-amnesty, pylint: disable=no-else-raise
            raise ValidationError({"endorsed": ["This field is required for question threads."]})
        elif endorsed:
            # CS does not apply resp_skip and resp_limit to endorsed responses
            # of a question post
            responses = cc_thread["endorsed_responses"][response_skip:(response_skip + page_size)]
            resp_total = len(cc_thread["endorsed_responses"])
        else:
            responses = cc_thread["non_endorsed_responses"]
            resp_total = cc_thread["non_endorsed_resp_total"]
    else:
        if endorsed is not None:
            raise ValidationError(
                {"endorsed": ["This field may not be specified for discussion threads."]}
            )
        responses = cc_thread["children"]
        resp_total = cc_thread["resp_total"]

    # The comments service returns the last page of results if the requested
    # page is beyond the last page, but we want be consistent with DRF's general
    # behavior and return a PageNotFoundError in that case
    if not responses and page != 1:
        raise PageNotFoundError("Page not found (No results on this page).")
    num_pages = (resp_total + page_size - 1) // page_size if resp_total else 1

    results = _serialize_discussion_entities(request, context, responses, requested_fields, DiscussionEntity.comment)

    paginator = DiscussionAPIPagination(request, page, num_pages, resp_total)
    return paginator.get_paginated_response(results)


def _check_fields(allowed_fields, data, message):
    """
    Checks that the keys given in data is in allowed_fields

    Arguments:
        allowed_fields (set): A set of allowed fields
        data (dict): The data to compare the allowed_fields against
        message (str): The message to return if there are any invalid fields

    Raises:
        ValidationError if the given data contains a key that is not in
            allowed_fields
    """
    non_allowed_fields = {field: [message] for field in data.keys() if field not in allowed_fields}
    if non_allowed_fields:
        raise ValidationError(non_allowed_fields)


def _check_initializable_thread_fields(data, context):
    """
    Checks if the given data contains a thread field that is not initializable
    by the requesting user

    Arguments:
        data (dict): The data to compare the allowed_fields against
        context (dict): The context appropriate for use with the thread which
            includes the requesting user

    Raises:
        ValidationError if the given data contains a thread field that is not
            initializable by the requesting user
    """
    _check_fields(
        get_initializable_thread_fields(context),
        data,
        "This field is not initializable."
    )


def _check_initializable_comment_fields(data, context):
    """
    Checks if the given data contains a comment field that is not initializable
    by the requesting user

    Arguments:
        data (dict): The data to compare the allowed_fields against
        context (dict): The context appropriate for use with the comment which
            includes the requesting user

    Raises:
        ValidationError if the given data contains a comment field that is not
            initializable by the requesting user
    """
    _check_fields(
        get_initializable_comment_fields(context),
        data,
        "This field is not initializable."
    )


def _check_editable_fields(cc_content, data, context):
    """
    Raise ValidationError if the given update data contains a field that is not
    editable by the requesting user
    """
    _check_fields(
        get_editable_fields(cc_content, context),
        data,
        "This field is not editable."
    )


def _do_extra_actions(api_content, cc_content, request_fields, actions_form, context, request):
    """
    Perform any necessary additional actions related to content creation or
    update that require a separate comments service request.
    """
    for field, form_value in actions_form.cleaned_data.items():
        if field in request_fields and form_value != api_content[field]:
            api_content[field] = form_value
            if field == "following":
                _handle_following_field(form_value, context["cc_requester"], cc_content)
            elif field == "abuse_flagged":
                _handle_abuse_flagged_field(form_value, context["cc_requester"], cc_content)
            elif field == "voted":
                _handle_voted_field(form_value, cc_content, api_content, request, context)
            elif field == "read":
                _handle_read_field(api_content, form_value, context["cc_requester"], cc_content)
            elif field == "pinned":
                _handle_pinned_field(form_value, cc_content, context["cc_requester"])
            else:
                raise ValidationError({field: ["Invalid Key"]})


def _handle_following_field(form_value, user, cc_content):
    """follow/unfollow thread for the user"""
    if form_value:
        user.follow(cc_content)
    else:
        user.unfollow(cc_content)


def _handle_abuse_flagged_field(form_value, user, cc_content):
    """mark or unmark thread/comment as abused"""
    if form_value:
        cc_content.flagAbuse(user, cc_content)
    else:
        cc_content.unFlagAbuse(user, cc_content, removeAll=False)


def _handle_voted_field(form_value, cc_content, api_content, request, context):
    """vote or undo vote on thread/comment"""
    signal = thread_voted if cc_content.type == 'thread' else comment_voted
    signal.send(sender=None, user=context["request"].user, post=cc_content)
    if form_value:
        context["cc_requester"].vote(cc_content, "up")
        api_content["vote_count"] += 1
    else:
        context["cc_requester"].unvote(cc_content)
        api_content["vote_count"] -= 1
    track_voted_event(
        request, context["course"], cc_content, vote_value="up", undo_vote=False if form_value else True  # lint-amnesty, pylint: disable=simplifiable-if-expression
    )


def _handle_read_field(api_content, form_value, user, cc_content):
    """
    Marks thread as read for the user
    """
    if form_value and not cc_content['read']:
        user.read(cc_content)
        # When a thread is marked as read, all of its responses and comments
        # are also marked as read.
        api_content["unread_comment_count"] = 0


def _handle_pinned_field(pin_thread: bool, cc_content: Thread, user: User):
    """
    Pins or unpins a thread

    Arguments:

        pin_thread (bool): Value of field from API
        cc_content (Thread): The thread on which to operate
        user (User): The user performing the action
    """
    if pin_thread:
        cc_content.pin(user, cc_content.id)
    else:
        cc_content.un_pin(user, cc_content.id)


def create_thread(request, thread_data):
    """
    Create a thread.

    Arguments:

        request: The django request object used for build_absolute_uri and
          determining the requesting user.

        thread_data: The data for the created thread.

    Returns:

        The created thread; see discussion.rest_api.views.ThreadViewSet for more
        detail.
    """
    course_id = thread_data.get("course_id")
    user = request.user
    if not course_id:
        raise ValidationError({"course_id": ["This field is required."]})
    try:
        course_key = CourseKey.from_string(course_id)
        course = _get_course(course_key, user)
    except InvalidKeyError as err:
        raise ValidationError({"course_id": ["Invalid value."]}) from err

    if not discussion_open_for_user(course, user):
        raise DiscussionBlackOutException

    context = get_context(course, request)
    _check_initializable_thread_fields(thread_data, context)
    discussion_settings = CourseDiscussionSettings.get(course_key)
    if (
            "group_id" not in thread_data and
            is_commentable_divided(course_key, thread_data.get("topic_id"), discussion_settings)
    ):
        thread_data = thread_data.copy()
        thread_data["group_id"] = get_group_id_for_user(user, discussion_settings)
    serializer = ThreadSerializer(data=thread_data, context=context)
    actions_form = ThreadActionsForm(thread_data)
    if not (serializer.is_valid() and actions_form.is_valid()):
        raise ValidationError(dict(list(serializer.errors.items()) + list(actions_form.errors.items())))
    serializer.save()
    cc_thread = serializer.instance
    thread_created.send(sender=None, user=user, post=cc_thread)
    api_thread = serializer.data
    _do_extra_actions(api_thread, cc_thread, list(thread_data.keys()), actions_form, context, request)

    track_thread_created_event(request, course, cc_thread, actions_form.cleaned_data["following"])

    return api_thread


def create_comment(request, comment_data):
    """
    Create a comment.

    Arguments:

        request: The django request object used for build_absolute_uri and
          determining the requesting user.

        comment_data: The data for the created comment.

    Returns:

        The created comment; see discussion.rest_api.views.CommentViewSet for more
        detail.
    """
    thread_id = comment_data.get("thread_id")
    if not thread_id:
        raise ValidationError({"thread_id": ["This field is required."]})
    cc_thread, context = _get_thread_and_context(request, thread_id)

    course = context["course"]
    if not discussion_open_for_user(course, request.user):
        raise DiscussionBlackOutException

    # if a thread is closed; no new comments could be made to it
    if cc_thread["closed"]:
        raise PermissionDenied

    _check_initializable_comment_fields(comment_data, context)
    serializer = CommentSerializer(data=comment_data, context=context)
    actions_form = CommentActionsForm(comment_data)
    if not (serializer.is_valid() and actions_form.is_valid()):
        raise ValidationError(dict(list(serializer.errors.items()) + list(actions_form.errors.items())))
    serializer.save()
    cc_comment = serializer.instance
    comment_created.send(sender=None, user=request.user, post=cc_comment)
    api_comment = serializer.data
    _do_extra_actions(api_comment, cc_comment, list(comment_data.keys()), actions_form, context, request)

    track_comment_created_event(request, course, cc_comment, cc_thread["commentable_id"], followed=False)

    return api_comment


def update_thread(request, thread_id, update_data):
    """
    Update a thread.

    Arguments:

        request: The django request object used for build_absolute_uri and
          determining the requesting user.

        thread_id: The id for the thread to update.

        update_data: The data to update in the thread.

    Returns:

        The updated thread; see discussion.rest_api.views.ThreadViewSet for more
        detail.
    """
    cc_thread, context = _get_thread_and_context(request, thread_id, retrieve_kwargs={"with_responses": True})
    _check_editable_fields(cc_thread, update_data, context)
    serializer = ThreadSerializer(cc_thread, data=update_data, partial=True, context=context)
    actions_form = ThreadActionsForm(update_data)
    if not (serializer.is_valid() and actions_form.is_valid()):
        raise ValidationError(dict(list(serializer.errors.items()) + list(actions_form.errors.items())))
    # Only save thread object if some of the edited fields are in the thread data, not extra actions
    if set(update_data) - set(actions_form.fields):
        serializer.save()
        # signal to update Teams when a user edits a thread
        thread_edited.send(sender=None, user=request.user, post=cc_thread)
    api_thread = serializer.data
    _do_extra_actions(api_thread, cc_thread, list(update_data.keys()), actions_form, context, request)

    # always return read as True (and therefore unread_comment_count=0) as reasonably
    # accurate shortcut, rather than adding additional processing.
    api_thread['read'] = True
    api_thread['unread_comment_count'] = 0
    return api_thread


def update_comment(request, comment_id, update_data):
    """
    Update a comment.

    Arguments:

        request: The django request object used for build_absolute_uri and
          determining the requesting user.

        comment_id: The id for the comment to update.

        update_data: The data to update in the comment.

    Returns:

        The updated comment; see discussion.rest_api.views.CommentViewSet for more
        detail.

    Raises:

        CommentNotFoundError: if the comment does not exist or is not accessible
        to the requesting user

        PermissionDenied: if the comment is accessible to but not editable by
          the requesting user

        ValidationError: if there is an error applying the update (e.g. raw_body
          is empty or thread_id is included)
    """
    cc_comment, context = _get_comment_and_context(request, comment_id)
    _check_editable_fields(cc_comment, update_data, context)
    serializer = CommentSerializer(cc_comment, data=update_data, partial=True, context=context)
    actions_form = CommentActionsForm(update_data)
    if not (serializer.is_valid() and actions_form.is_valid()):
        raise ValidationError(dict(list(serializer.errors.items()) + list(actions_form.errors.items())))
    # Only save comment object if some of the edited fields are in the comment data, not extra actions
    if set(update_data) - set(actions_form.fields):
        serializer.save()
        comment_edited.send(sender=None, user=request.user, post=cc_comment)
    api_comment = serializer.data
    _do_extra_actions(api_comment, cc_comment, list(update_data.keys()), actions_form, context, request)
    return api_comment


def get_thread(request, thread_id, requested_fields=None):
    """
    Retrieve a thread.

    Arguments:

        request: The django request object used for build_absolute_uri and
          determining the requesting user.

        thread_id: The id for the thread to retrieve

        requested_fields: Indicates which additional fields to return for
        thread. (i.e. ['profile_image'])
    """
    # Possible candidate for optimization with caching:
    #   Param with_responses=True required only to add "response_count" to response.
    cc_thread, context = _get_thread_and_context(
        request,
        thread_id,
        retrieve_kwargs={
            "with_responses": True,
            "user_id": str(request.user.id),
        }
    )
    return _serialize_discussion_entities(request, context, [cc_thread], requested_fields, DiscussionEntity.thread)[0]


def get_response_comments(request, comment_id, page, page_size, requested_fields=None):
    """
    Return the list of comments for the given thread response.

    Arguments:

        request: The django request object used for build_absolute_uri and
          determining the requesting user.

        comment_id: The id of the comment/response to get child comments for.

        page: The page number (1-indexed) to retrieve

        page_size: The number of comments to retrieve per page

        requested_fields: Indicates which additional fields to return for
        each child comment. (i.e. ['profile_image'])

    Returns:

        A paginated result containing a list of comments

    """
    try:
        cc_comment = Comment(id=comment_id).retrieve()
        cc_thread, context = _get_thread_and_context(
            request,
            cc_comment["thread_id"],
            retrieve_kwargs={
                "with_responses": True,
                "recursive": True,
            }
        )
        if cc_thread["thread_type"] == "question":
            thread_responses = itertools.chain(cc_thread["endorsed_responses"], cc_thread["non_endorsed_responses"])
        else:
            thread_responses = cc_thread["children"]
        response_comments = []
        for response in thread_responses:
            if response["id"] == comment_id:
                response_comments = response["children"]
                break

        response_skip = page_size * (page - 1)
        paged_response_comments = response_comments[response_skip:(response_skip + page_size)]
        if not paged_response_comments and page != 1:
            raise PageNotFoundError("Page not found (No results on this page).")

        results = _serialize_discussion_entities(
            request, context, paged_response_comments, requested_fields, DiscussionEntity.comment
        )

        comments_count = len(response_comments)
        num_pages = (comments_count + page_size - 1) // page_size if comments_count else 1
        paginator = DiscussionAPIPagination(request, page, num_pages, comments_count)
        return paginator.get_paginated_response(results)
    except CommentClientRequestError as err:
        raise CommentNotFoundError("Comment not found") from err


def delete_thread(request, thread_id):
    """
    Delete a thread.

    Arguments:

        request: The django request object used for build_absolute_uri and
          determining the requesting user.

        thread_id: The id for the thread to delete

    Raises:

        PermissionDenied: if user does not have permission to delete thread

    """
    cc_thread, context = _get_thread_and_context(request, thread_id)
    if can_delete(cc_thread, context):
        cc_thread.delete()
        thread_deleted.send(sender=None, user=request.user, post=cc_thread)
    else:
        raise PermissionDenied


def delete_comment(request, comment_id):
    """
    Delete a comment.

    Arguments:

        request: The django request object used for build_absolute_uri and
          determining the requesting user.

        comment_id: The id of the comment to delete

    Raises:

        PermissionDenied: if user does not have permission to delete thread

    """
    cc_comment, context = _get_comment_and_context(request, comment_id)
    if can_delete(cc_comment, context):
        cc_comment.delete()
        comment_deleted.send(sender=None, user=request.user, post=cc_comment)
    else:
        raise PermissionDenied
