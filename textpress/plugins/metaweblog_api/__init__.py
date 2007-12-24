# -*- coding: utf-8 -*-
"""
    textpress.plugins.metaweblog_api
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Adds support for the MetaWeblog API to TextPress.

    :copyright: 2007 by Armin Ronacher.
    :license: GNU GPL.
"""
import sys
from textpress.api import *
from textpress.models import User, Post, Tag, ROLE_AUTHOR, STATUS_DRAFT, \
     STATUS_PUBLISHED
from textpress.utils import format_iso8601, parse_iso8601, XMLRPC
from datetime import datetime


class APIError(Exception):
    """Any errors occoured in the api."""


def export(name, fetch_user=True):
    """Decorator to mark a function as exported."""
    def wrapped(f):
        def proxy(*args, **kwargs):
            # if fetch user is true the second and third
            # parameter are threated as username and password
            # and transformed into an user object.
            if fetch_user:
                username, password = args[1:3]
                user = User.objects.filter_by(username=username).first()
                if user is None:
                    raise APIError('user not found')
                elif not user.check_password(password):
                    raise APIError('wrong password')
                args = args[:1] + (user,) + args[3:]
            rv = f(*args, **kwargs)
            if rv is None:
                return False
            return rv
        proxy.__name__ = f.__name__
        proxy.__doc__ = f.__doc__
        xmlrpc.register_function(proxy, name)
        return proxy
    return wrapped


xmlrpc = XMLRPC()


@export('metaWeblog.newPost')
def new_post(blog_id, user, struct, publish):
    """Create a new post."""
    if user.role < ROLE_AUTHOR:
        raise APIError('sorry, you cannot post in this weblog')

    if struct.get('dateCreated'):
        pub_date = parse_iso8601(struct['dateCreated'])
    else:
        pub_date = None

    if publish:
        status = STATUS_PUBLISHED
    else:
        status = STATUS_DRAFT

    post = Post(struct.get('title', ''), user, struct.get('description', ''),
                pub_date=pub_date, status=status)

    for tag in struct.get('categories') or ():
        post.tags.append(Tag.objects.get_or_create(tag))

    db.flush()
    return post.post_id


@export('metaWeblog.getPost')
def get_post(post_id, user):
    post = Post.objects.get(post_id)
    if post is None:
        raise APIError('post does not exist')
    elif not post.can_access(user):
        raise APIError('you cannot access this post')
    link = url_for(post, _external=True)

    return {
        'postid':           post.post_id,
        'title':            post.title,
        'userid':           post.author.user_id,
        'username':         post.author.username,
        'displayName':      post.author.display_name,
        'dateCreated':      format_iso8601(post.pub_date),
        'description':      post.raw_body,
        'excerpt':          post.raw_intro or '',
        'link':             link,
        'permaLink':        link,
        'categories':       [x.slug for x in post.tags],
    }


def setup(app, plugin):
    app.add_api('metaweblog', True, xmlrpc)
