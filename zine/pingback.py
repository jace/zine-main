# -*- coding: utf-8 -*-
"""
    zine.pingback
    ~~~~~~~~~~~~~

    This module implements the pingback API and a function to emit pingbacks
    to different blogs.  The implementation here is a `Pingback 1.0`_
    implementation, compatible to the Pingback specification by Ian Hickson.

    .. _Pingback 1.0: http://www.hixie.ch/specs/pingback/pingback-1.0

    Note that pingback support is implemented in the `Zine` core and
    can't be removed.  You can however disable it in the configuration if
    you want.  Plugins can hook into the pingback system by registering
    a callback for an URL endpoint using `app.add_pingback_endpoint` during
    the application setup.

    Important
    =========

    Due to a broken design for trackback we will *never* support trackbacks
    in the `Zine` core.  Neither do we handle incoming trackbacks, nor
    do we emit trackbacks.


    :copyright: (c) 2009 by the Zine Team, see AUTHORS for more details.
    :license: BSD, see LICENSE for more details.
"""
import re
from xmlrpclib import ServerProxy, Fault

from werkzeug.routing import RequestRedirect, NotFound
from werkzeug import escape, unescape

from zine.api import get_request, get_application, url_for, db, _
from zine.models import Post, Comment
from zine.utils.exceptions import UserException
from zine.utils.xml import XMLRPC, strip_tags
from zine.utils.net import open_url, NetException


_title_re = re.compile(r'<title>(.*?)</title>(?i)')
_pingback_re = re.compile(r'<link rel="pingback" href="([^"]+)" ?/?>(?i)')
_chunk_re = re.compile(r'\n\n|<(?:p|div|h\d)[^>]*>')


class PingbackError(UserException):
    """Raised if the remote server caused an exception while pingbacking.
    This is not raised if the pingback function is unable to locate a
    remote server.
    """

    _ = lambda x: x
    default_messages = {
        16: _(u'source URL does not exist'),
        17: _(u'The source URL does not contain a link to the target URL'),
        32: _(u'The specified target URL does not exist'),
        33: _(u'The specified target URL cannot be used as a target'),
        48: _(u'The pingback has already been registered'),
        49: _(u'Access Denied')
    }
    del _

    def __init__(self, fault_code, internal_message=None):
        UserException.__init__(self)
        self.fault_code = fault_code
        self._internal_message = internal_message

    def as_fault(self):
        """Return the pingback errors XMLRPC fault."""
        return Fault(self.fault_code, self.internal_message or
                     'unknown server error')

    @property
    def ignore_silently(self):
        """If the error can be ignored silently."""
        return self.fault_code in (17, 33, 48, 49)

    @property
    def means_missing(self):
        """If the error means that the resource is missing or not
        accepting pingbacks.
        """
        return self.fault_code in (32, 33)

    @property
    def internal_message(self):
        if self._internal_message is not None:
            return self._internal_message
        return self.default_messages.get(self.fault_code) or 'server error'

    @property
    def message(self):
        msg = self.default_messages.get(self.fault_code)
        if msg is not None:
            return _(msg)
        return _(u'An unknown server error (%s) occurred') % self.fault_code


def pingback(source_uri, target_uri):
    """Try to notify the server behind `target_uri` that `source_uri`
    points to `target_uri`.  If that fails an `PingbackError` is raised.
    """
    try:
        response = open_url(target_uri)
    except:
        raise PingbackError(32)

    try:
        pingback_uri = response.headers['X-Pingback']
    except KeyError:
        match = _pingback_re.search(response.data)
        if match is None:
            raise PingbackError(33)
        pingback_uri = unescape(match.group(1))

    rpc = ServerProxy(pingback_uri)
    try:
        return rpc.pingback.ping(source_uri, target_uri)
    except Fault, e:
        raise PingbackError(e.faultCode)
    except:
        raise PingbackError(32)


def handle_pingback_request(source_uri, target_uri):
    """This method is exported via XMLRPC as `pingback.ping` by the
    pingback API.
    """
    app = get_application()

    # next we check if the source URL does indeed exist
    try:
        response = open_url(source_uri)
    except NetException:
        raise Fault(16, 'The source URL does not exist.')

    # we only accept pingbacks for links below our blog URL
    blog_url = app.cfg['blog_url']
    if not blog_url.endswith('/'):
        blog_url += '/'
    if not target_uri.startswith(blog_url):
        raise Fault(32, 'The specified target URL does not exist.')
    path_info = target_uri[len(blog_url):]
    handler = endpoint = values = None

    while 1:
        try:
            endpoint, values = app.url_adapter.match(path_info)
        except RequestRedirect, e:
            path_info = e.new_url[len(blog_url):]
        except NotFound, e:
            break
        else:
            if endpoint in app.pingback_endpoints:
                handler = app.pingback_endpoints[endpoint]

    # if we have an endpoint based handler use that one first
    raise_later = None
    if handler is not None:
        try:
            handler(response, target_uri, **values)
        except PingbackError, e:
            raise_later = e

    # if the handler was none or an acception happend in the
    if handler is None or (raise_later is not None and
                           raise_later.means_missing):
        for handler in app.pingback_url_handlers:
            try:
                if handler(response, target_uri, path_info):
                    raise_later = None
                    break
            except PingbackError, e:
                raise_later = e
                # fatal error, abort
                if not raise_later.means_missing:
                    break
        else:
            raise_later = PingbackError(33)

    # now if we have an exception raise it as XMLRPC fault
    if raise_later is not None:
        raise raise_later.as_fault()

    # return some debug info
    return u'\n'.join((
        'endpoint: %r',
        'values: %r',
        'path_info: %r',
        'source_uri: %s',
        'target_uri: %s',
        'handler: %r'
    )) % (endpoint, values, path_info, source_uri, target_uri, handler)


def get_excerpt(response, url_hint, body_limit=1024 * 512):
    """Get an excerpt from the given `response`.  `url_hint` is the URL
    which will be used as anchor for the excerpt.  The return value is a
    tuple in the form ``(title, body)``.  If one of the two items could
    not be calculated it will be `None`.
    """
    if isinstance(response, basestring):
        response = open_url(response)
    contents = response.data[:body_limit]
    title_match = _title_re.search(contents)
    title = title_match and strip_tags(title_match.group(1)) or None

    link_re = re.compile(r'<a[^>]+?"\s*%s\s*"[^>]*>(.*?)</a>(?is)' %
                         re.escape(url_hint))
    for chunk in _chunk_re.split(contents):
        match = link_re.search(chunk)
        if not match:
            continue
        before = chunk[:match.start()]
        after = chunk[match.end():]
        raw_body = '%s\0%s' % (strip_tags(before).replace('\0', ''),
                               strip_tags(after).replace('\0', ''))
        body_match = re.compile(r'(?:^|\b)(.{0,120})\0(.{0,120})(?:\b|$)') \
                       .search(raw_body)
        if body_match:
            break
    else:
        return title, None

    before, after = body_match.groups()
    link_text = strip_tags(match.group(1))
    if len(link_text) > 60:
        link_text = link_text[:60] + u' …'

    bits = before.split()
    bits.append(link_text)
    bits.extend(after.split())
    return title, u'[…] %s […]' % u' '.join(bits)


def inject_header(f):
    """Decorate a view function with this function to automatically set the
    `X-Pingback` header if the status code is 200.
    """
    def oncall(*args, **kwargs):
        rv = f(*args, **kwargs)
        if rv.status_code == 200:
            rv.headers['X-Pingback'] = url_for('services/pingback',
                                               _external=True)
        return rv
    oncall.__name__ = f.__name__
    oncall.__module__ = f.__module__
    oncall.__doc__ = f.__doc__
    return oncall


def pingback_post(response, target_uri, slug):
    """This is the pingback handler for posts."""
    post = Post.query.filter_by(slug=slug).first()
    if post is None:
        return False

    if post is None or not post.pings_enabled:
        raise PingbackError(33, 'no such post')
    elif not post.can_read():
        raise PingbackError(49, 'access denied')
    title, excerpt = get_excerpt(response, target_uri)
    if not title:
        raise PingbackError(17, 'no title provided')
    elif not excerpt:
        raise PingbackError(17, 'no useable link to target')
    old_pingback = Comment.query.filter(
        (Comment.is_pingback == True) &
        (Comment.www == response.url)
    ).first()
    if old_pingback:
        raise PingbackError(48, 'pingback has already been registered')
    Comment(post, title, excerpt, '', response.url, is_pingback=True,
            submitter_ip=get_request().remote_addr, parser='text')
    db.commit()
    return True


# the pingback service the application registers on creation
service = XMLRPC()
service.register_function(handle_pingback_request, 'pingback.ping')

# a dict of default pingback endpoints (non plugin endpoints)
# these are used as defaults for pingback endpoints on startup
endpoints = {}

# a dict of default pingback URL handlers (non plugin handlers)
# that are called one after another to find out if a yet unhandled
# URL reacts to pingbacks.
url_handlers = [pingback_post]
