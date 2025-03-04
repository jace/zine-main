# -*- coding: utf-8 -*-
"""
    zine.services
    ~~~~~~~~~~~~~

    The builtin (JSON) services.

    :copyright: (c) 2009 by the Zine Team, see AUTHORS for more details.
    :license: BSD, see LICENSE for more details.
"""
from werkzeug import abort

from zine.api import *
from zine.models import Comment
from zine.privileges import MODERATE_COMMENTS


def do_get_comment(req):
    comment_id = req.values.get('comment_id')
    if comment_id is None:
        abort(404)
    comment = Comment.query.get(comment_id)
    if comment is None:
        abort(404)
    if comment.blocked and not req.user.has_privilege(MODERATE_COMMENTS):
        abort(403)
    if comment.parent is not None:
        parent_id = comment.parent.id
    else:
        parent_id = None
    email = None
    if req.user.is_manager:
        email = comment.email
    return {
        'id':           comment.id,
        'parent':       parent_id,
        'body':         unicode(comment.body),
        'author':       comment.author,
        'email':        email,
        'pub_date':     int(comment.pub_date.strftime('%s')),
    }


all_services = {
    'get_comment':          do_get_comment
}
