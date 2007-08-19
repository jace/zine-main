# -*- coding: utf-8 -*-
"""
    textpress.application
    ~~~~~~~~~~~~~~~~~~~~~

    The main application.

    :copyright: 2007 by Armin Ronacher.
    :license: GNU GPL.
"""
from os import path
from threading import local, Lock
from itertools import izip
from datetime import datetime

from textpress.database import sessions, db, upgrade_database
from textpress.config import Configuration
from textpress.utils import gen_sid, format_datetime, format_date, \
     format_month

from werkzeug.utils import SharedDataMiddleware
from werkzeug.wrappers import BaseRequest, BaseResponse
from werkzeug import routing

from jinja import Environment
from jinja.loaders import BaseLoader, CachedLoaderMixin
from jinja.exceptions import TemplateNotFound
from jinja.datastructure import Deferred


#: path to the shared data of the core components
SHARED_DATA = path.join(path.dirname(__file__), 'shared')

#: path to the builtin templates (admin panel and page defaults)
BUILTIN_TEMPLATE_PATH = path.join(path.dirname(__file__), 'templates')

#: helds all the thread local variables
#: currently those are:
#:
#: `app`:
#:      reference to the application in the current thread
#: `req`:
#:      references to the current request if there is one
#: `page_metadata`:
#:      list of tuples containing the metadata for the page.
#:      this is only set if there is an request processed.
_locals = local()

#: the lock for the application setup
_setup_lock = Lock()

#: because events are not application bound we keep them
#: unique by sharing them. For better performance we don't
#: lock the access to this variable, it's unlikely that
#: two applications connect a new event in the same millisecond
#: because connecting happens during application startup and
#: there is a global application setup lock.
_next_listener_id = 0


def emit_event(event, *args, **kw):
    """Emit an event and return a `EventResult` instance."""
    buffered = kw.pop('buffered', False)
    if kw:
        raise TypeError('got invalid keyword argument %r' % iter(kw).next())
    mgr = _locals.app._event_manager
    return EventResult(mgr.emit(event, args), buffered)


def abort(code=404):
    """Return to the application with a not found response."""
    if code == 404:
        resp = render_response('404.html')
        resp.status = 404
    elif code == 403:
        if _locals.req.user.is_somebody:
            resp = render_response('403.html')
            resp.status = 403
        else:
            resp = Response('You have to login before accessing '
                            'this resource.', mimetype='text/plain',
                            status=302)
            resp.headers['Location'] = url_for('admin/login')
    else:
        msg = Response('Error %d' % code, mimetype='text/plain', status=code)
    raise DirectResponse(resp)


def redirect(url, status=302, allow_external_redirect=False):
    """Return to the application with a redirect response."""
    # XXX: make the url external and make sure that the redirects
    # are only internal except allow_external_redirect is enabled
    resp = Response('Moved to %s' % url, mimetype='text/plain',
                    status=status)
    resp.headers['Location'] = url
    raise DirectResponse(resp)


def url_for(endpoint, _external=False, **args):
    """Get the url to an endpoint."""
    if hasattr(endpoint, 'get_url_values'):
        rv = endpoint.get_url_values()
        if rv is not None:
            endpoint, updated_args = rv
            args.update(updated_args)
    return _locals.req.urls.build(endpoint, args, _external)


def get_request():
    """Get the current request."""
    return getattr(_locals, 'req', None)


def get_application():
    """Get the current application."""
    return getattr(_locals, 'app', None)


def add_link(rel, href, type, title=None, charset=None, media=None):
    """Add a new link to the metadata of the current page being processed."""
    _locals.page_metadata.append(('link', locals()))


def add_meta(http_equiv=None, name=None, content=None):
    """Add a new meta element to the metadata of the current page."""
    _locals.page_metadata.append(('meta', locals()))


def add_script(src, type='text/javascript'):
    """Load a script."""
    _locals.page_metadata.append(('script', locals()))


def add_header_snippet(html):
    """Add some HTML as header snippet."""
    _locals.page_metadata.append(('snippet', {'html': html}))


def render_template(template_name, _stream=False, **context):
    """
    Renders a template. If `_stream` is ``True`` the return value will be
    a Jinja template stream and not an unicode object.
    This is used by `render_response`.
    """
    emit_event('before-render-template', template_name, _stream, context,
               buffered=True)
    tmpl = _locals.app.template_env.get_template(template_name)
    if _stream:
        return tmpl.stream(context)
    return tmpl.render(context)


def render_response(template_name, **context):
    """
    Like render_template but returns a response. If `_stream` is ``True``
    the response returned uses the Jinja stream processing. This is useful
    for pages with lazy generated content or huge output where you don't
    want the users to wait until the calculation ended. Use streaming only
    in those situations because it's usually slower than bunch processing.
    """
    return Response(render_template(template_name, **context))


def require_role(role):
    """Wrap a view so that it requires a given role to access."""
    def wrapped(f):
        def decorated(req, **kwargs):
            if req.user.role >= role:
                return f(req, **kwargs)
            abort(403)
        decorated.__name__ = f.__name__
        decorated.__doc__ = f.__doc__
        return decorated
    return wrapped


class Request(BaseRequest):
    """The used request class."""
    charset = 'utf-8'

    def __init__(self, app, environ):
        super(Request, self).__init__(environ)
        self.app = app
        self.urls = app.url_map.bind_to_environ(environ)

        # get the session
        from textpress.models import User
        user = session = last_change = None
        sid = self.cookies.get(app.cfg['sid_cookie_name'])
        if sid is None:
            sid = gen_sid()
        else:
            query = sessions.select(sessions.c.sid == sid)
            row = app.database_engine.execute(query).fetchone()
            if row is not None:
                session = row.data
                if row.user_id:
                    user = User.get(row.user_id)
                last_change = row.last_change
        if user is None:
            user = User.get_nobody()
        self.sid = sid
        self.user = self._old_user = user
        self.session = session or {}
        self.last_session_update = last_change

    def login(self, user):
        """Log the given user in. Can be user_id, username or
        a full blown user object."""
        from textpress.models import User
        if isinstance(user, (int, long)):
            user = User.get(user)
        elif isinstance(user, basestring):
            user = User.get_by(username=user)
        if user is None:
            raise RuntimeError('User does not exist')
        self.user = user
        emit_event('after-user-login', user, buffered=True)

    def logout(self):
        """Log the current user out."""
        from textpress.models import User
        user = self.user
        self.user = User.get_nobody()
        self.session.clear()
        emit_event('after-user-logout', user, buffered=True)

    def save_session(self):
        """Save the session if required. Return True if it was saved."""
        if self.user != self._old_user or self.session:
            just_update = self.last_session_update is not None
            last_change = self.last_session_update = datetime.utcnow()
            if just_update:
                q = sessions.c.sid == self.sid
                self.app.database_engine.execute(sessions.update(q),
                    user_id=self.user.user_id,
                    data=self.session,
                    last_change=last_change
                )
            else:
                self.app.database_engine.execute(sessions.insert(),
                    sid=self.sid,
                    user_id=self.user.user_id,
                    last_change=last_change,
                    data=self.session
                )
            self._old_user = self.user
            return True
        return False


class Response(BaseResponse):
    """
    An utf-8 response, with text/html as default mimetype.
    Makes sure that the session is saved on request end.
    """
    charset = 'utf-8'
    default_mimetype = 'text/html'

    def __call__(self, environ, start_response):
        req = environ['werkzeug.request']
        if req.save_session():
            self.set_cookie(req.app.cfg['sid_cookie_name'], req.sid)
        return super(Response, self).__call__(environ, start_response)


class NotFound(Exception):
    """Special exception that is raised to notify the app about a
    missing resource or page."""


class DirectResponse(Exception):
    """Raise this with a response as first argument to send a response."""

    def __init__(self, response):
        self.response = response
        Exception.__init__(self, response)


class EventManager(object):
    """
    Helper class that handles event listeners and events.

    This is *not* a public interface. Always use the emit_event()
    functions to access it or the connect_event() / disconnect_event()
    functions on the application.
    """

    def __init__(self, app):
        self.app = app
        self._listeners = {}

    def connect(self, event, callback):
        global _next_listener_id
        listener_id = _next_listener_id
        event = intern(event)
        if event not in self._listeners:
            self._listeners[event] = {listener_id: callback}
        else:
            self._listeners[event][listener_id] = callback
        _next_listener_id += 1
        return listener_id

    def remove(self, listener_id):
        for event in self._listeners:
            event.pop(listener_id, None)

    def emit(self, name, args):
        if name in self._listeners:
            for listener_id, cb in self._listeners[name].iteritems():
                yield listener_id, cb(*args)


class EventResult(object):
    """Wraps a generator for the emit_event() function."""

    __slots__ = ('_participated_listeners', '_results', '_gen', 'buffered')

    def __init__(self, gen, buffered):
        if buffered:
            items = list(gen)
            self._participated_listeners = set(x[0] for x in items)
            self._results = [x[1] for x in items]
        else:
            self._gen = gen
        self.buffered = buffered

    @property
    def participated_listeners(self):
        if not hasattr(self, '_participated_listeners'):
            tuple(self)
        return self._participated_listeners

    @property
    def results(self):
        if not hasattr(self, '_results'):
            tuple(self)
        return self._results

    def __iter__(self):
        if self.buffered:
            for item in self.results:
                yield item
        elif not hasattr(self, '_results'):
            self._results = []
            self._participated_listeners = []
            for listener_id, result in self._gen:
                self._results.append(result)
                self._participated_listeners.append(listener_id)
                yield result

    def __repr__(self):
        if self.buffered:
            detail = '- buffered [%d participants]' % len(self.results)
        else:
            detail = '(dynamic)'
        return '<EventResult %s>' % detail


class Theme(object):
    """
    Represents a theme and is created automaticall by `add_theme`
    """

    __slots__ = ('app', 'name', 'template_path', 'metadata')

    def __init__(self, app, name, template_path, metadata=None):
        self.app = app
        self.name = name
        self.template_path = template_path
        self.metadata = metadata or {}

    @property
    def preview_url(self):
        if self.metadata.get('preview'):
            endpoint, filename = self.metadata['preview'].split('::')
            return url_for(endpoint + '/shared', filename=filename)

    @property
    def has_preview(self):
        return bool(self.metadata.get('preview'))

    @property
    def detail_name(self):
        return self.metadata.get('name') or self.name.title()


class ThemeLoader(BaseLoader, CachedLoaderMixin):
    """
    Loads the templates. First it tries to load the templates of the
    current theme, if that doesn't work it loads the templates from the
    builtin template folder (contains base layout templates and admin
    templates).
    """

    def __init__(self, app):
        self.app = app
        CachedLoaderMixin.__init__(self,
            False,      # don't use memory caching
            40,         # for up to 40 templates
            None,       # path to disk cache
            False,      # don't reload templates
            app.instance_folder
        )

    def get_source(self, environment, name, parent):
        searchpath = []

        # if someone disabled the plugin that provided the current theme
        # we reset the config to "default"
        theme = self.app.cfg['theme']
        if theme not in self.app.themes:
            self.app.cfg['theme'] = theme = 'default'

        # if we have a real theme add the template path to the searchpath
        # on the highest position
        elif theme != 'default':
            searchpath.append(self.app.themes[theme].template_path)

        # add the template locations of the plugins
        searchpath.extend(self.app._template_searchpath)

        # now after the plugin searchpaths add the builtin one
        searchpath.append(BUILTIN_TEMPLATE_PATH)

        # now load the template from a searchpath or an event
        # provided searchpath. The events for the searchpath are
        # only fired if non of the builtin paths matched.
        parts = [x for x in name.split('/') if not x == '..']
        for fn in searchpath:
            fn = path.join(fn, *parts)
            if path.exists(fn):
                f = file(fn)
                try:
                    return f.read().decode(environment.template_charset)
                finally:
                    f.close()

        # if still nothing, raise an error
        raise TemplateNotFound(name)


class TextPress(object):
    """The WSGI application."""

    def __init__(self, instance_folder):
        # this check ensures that only make_app can create TextPress instances
        if getattr(_locals, 'app', None) is not self:
            raise TypeError('cannot create %r instances. use the make_app '
                            'factory function.' % self.__class__.__name__)

        # create the event manager, this is the first thing we have to
        # do because it could happen that events are sent during setup
        self._setup_finished = False
        self._event_manager = EventManager(self)

        # copy the dispatcher over so that we can apply middlewares
        self.dispatch_request = self._dispatch_request
        self.instance_folder = path.realpath(instance_folder)

        # config is in the database, but the config file that
        # points to the database is called "database.uri"
        self.database_uri_filename = path.join(instance_folder,
                                               'database.uri')

        # add a list for database checks
        self._database_checks = [upgrade_database]

        # and instanciate the configuration. this won't fail,
        # even if the database is not connected.
        self.cfg = Configuration(self)

        # if the application is not installed we replace the
        # dispatcher with a setup application
        database_uri = self.get_database_uri()
        if database_uri is None:
            from textpress.websetup import make_setup
            self.dispatch_request = make_setup(self)
            return

        # connect to the database, ignore errors for now
        self.connect_to_database(database_uri)

        # setup core package urls and shared stuff
        import textpress
        from textpress.api import _
        from textpress.urls import all_urls
        from textpress.views import all_views
        from textpress.services import all_services
        self._views = all_views.copy()
        self._url_rules = all_urls[:]
        self._services = all_services.copy()
        self._shared_exports = {}
        self._template_globals = {}
        self._template_filters = {}
        self._template_searchpath = []

        default_theme = Theme(self, 'default', BUILTIN_TEMPLATE_PATH, {
            'name':         _('Default Theme'),
            'summary':      _('Simple default theme that doesn\'t '
                              'contain any style information.'),
            'preview':      'core::default_preview.png'
        })
        self.themes = {'default': default_theme}
        self.apis = {}

        # load plugins
        from textpress.pluginsystem import find_plugins
        self.plugin_searchpath = [path.join(instance_folder, 'plugins')]
        self.plugins = {}
        for plugin in find_plugins(self):
            if plugin.active:
                plugin.setup()
            self.plugins[plugin.name] = plugin

        # check database integrity by performing the database checks
        if self.cfg['automatic_db_upgrade']:
            self.perform_database_upgrade()

        # init the template system with the core stuff
        from textpress import htmlhelpers, models
        env = Environment(loader=ThemeLoader(self))
        env.globals.update(
            req=Deferred(lambda *a: get_request()),
            cfg=self.cfg,
            h=htmlhelpers,
            url_for=url_for,
            get_post_archive_summary=models.get_post_archive_summary,
            get_post_list=models.get_post_list,
            get_tag_cloud=models.get_tag_cloud,
            get_page_metadata=self.get_page_metadata,
            textpress={
                'version':      textpress.__version__,
                'copyright':    '2007 by the Pocoo Team'
            }
        )
        env.filters.update(
            datetimeformat=lambda:lambda e, c, v: format_datetime(v),
            dateformat=lambda:lambda e, c, v: format_date(v),
            monthformat=lambda:lambda e, c, v: format_month(v)
        )

        # set up plugin template extensions
        env.globals.update(self._template_globals)
        env.filters.update(self._template_filters)
        del self._template_globals, self._template_filters
        self.template_env = env

        # now add the middleware for static file serving
        self.add_shared_exports('core', SHARED_DATA)
        self.add_middleware(SharedDataMiddleware, self._shared_exports)

        # set up the urls
        self.url_map = routing.Map(self._url_rules)
        del self._url_rules

        # mark the app as finished and send an event
        self._setup_finished = True
        emit_event('application-setup-done')

    def _reinit(self):
        """Calls init again. Dangerous. Don't do that! It's only used
        in the websetup and it's only save from there."""
        self.bind_to_thread()
        self.__init__(self.instance_folder)

    def bind_to_thread(self):
        """Bind the application to the current thread."""
        _locals.app = self

    def get_database_uri(self):
        """Get the database uri from the instance folder or return None."""
        if not path.exists(self.database_uri_filename):
            return
        f = file(self.database_uri_filename)
        try:
            return f.read().strip()
        finally:
            f.close()

    def set_database_uri(self, database_uri):
        """Store the database URI."""
        f = file(self.database_uri_filename, 'w')
        try:
            f.write(database_uri + '\n')
        finally:
            f.close()

    def connect_to_database(self, database_uri, perform_test=False):
        """Connect the app to the database defined."""
        self.database_engine = e = db.create_engine(database_uri)
        if perform_test:
            from sqlalchemy import select, literal
            e.execute(select([literal('foo')]))

    def perform_database_upgrade(self):
        """Do the database upgrade."""
        for check in self._database_checks:
            check(self)

    def add_template_filter(self, name, callback):
        """Add a template filter."""
        if self._setup_finished:
            raise RuntimeError('cannot add template filters '
                               'after application setup')
        self._template_filters[name] = callback

    def add_template_global(self, name, value, deferred=False):
        """Add a template global (or deferred factory function)."""
        if self._setup_finished:
            raise RuntimeError('cannot add template filters '
                               'after application setup')
        if deferred:
            value = Deferred(value)
        self._template_globals[name] = value

    def add_template_searchpath(self, path):
        """Add a new template searchpath to the application.
        This searchpath is queried *after* the themes but
        *before* the builtin templates are looked up."""
        if self._setup_finished:
            raise RuntimeError('cannot add template filters '
                               'after application setup')
        self._template_searchpath.append(path)

    def add_api(self, name, blog_id, preferred, callback):
        """Add a new API to the blog."""
        if self._setup_finished:
            raise RuntimeError('cannot add template filters '
                               'after application setup')
        endpoint = 'services/' + name
        self.apis[name] = (blog_id, preferred, endpoint)
        self.add_url_rule('/_services/' + name, endpoint=endpoint)
        self.add_view(endpoint, callback)

    def add_theme(self, name, template_path, metadata=None):
        """
        Add a theme. You have to provide the shortname for the theme
        which will be used in the admin panel etc. Then you have to provide
        the path for the templates. Usually this path is relative to the
        `__file__` directory.

        The metadata can be ommited but in that case some information in
        the admin panel is not available.
        """
        if self._setup_finished:
            raise RuntimeError('cannot add themes after application setup')
        self.themes[name] = Theme(self, name, template_path, metadata)

    def add_shared_exports(self, name, path):
        """
        Add a shared export for name that points to a given path and
        creates an url rule for <name>/shared that takes a filename
        parameter.
        """
        if self._setup_finished:
            raise RuntimeError('cannot add middlewares after '
                               'application setup')
        self._shared_exports['/_shared/' + name] = path
        self.add_url_rule('/_shared/%s/<string:filename>' % name,
                          endpoint=name + '/shared', build_only=True)

    def add_middleware(self, middleware_factory, *args, **kwargs):
        """Add a middleware to the application."""
        if self._setup_finished:
            raise RuntimeError('cannot add middlewares after '
                               'application setup')
        self.dispatch_request = middleware_factory(self.dispatch_request,
                                                   *args, **kwargs)

    def add_config_var(self, key, type, default):
        """Add a configuration variable to the application."""
        if self._setup_finished:
            raise RuntimeError('cannot add configuration values after '
                               'application setup')
        self.cfg.config_vars[key] = (type, default)

    def add_database_integrity_check(self, callback):
        """Allows plugins to perform database upgrades."""
        if self._setup_finished:
            raise RuntimeError('cannot add database integrity checks '
                               'after application setup')
        self._database_checks.append(callback)

    def add_url_rule(self, *args, **kwargs):
        """Add a new URL rule to the url map."""
        if self._setup_finished:
            raise RuntimeError('cannot add url rule after application setup')
        self._url_rules.append(routing.Rule(*args, **kwargs))

    def add_view(self, endpoint, callback):
        """Add a callback as view."""
        if self._setup_finished:
            raise RuntimeError('cannot add view after application setup')
        self._views[endpoint] = callback

    def add_servicepoint(self, identifier, callback):
        """Add a new function as servicepoint."""
        if self._setup_finished:
            raise RuntimeError('cannot add servicepoint after application setup')
        self._services[identifier] = callback

    def connect_event(self, event, callback):
        """Connect an event to the current application."""
        return self._event_manager.connect(event, callback)

    def disconnect_event(self, listener_id):
        """Disconnect a given listener_id."""
        self._event_manager.remove(listener_id)

    def get_view(self, endpoint):
        """Get the view for a given endpoint."""
        return self._views[endpoint]

    def get_page_metadata(self):
        """Return the metadata as HTML part for templates."""
        from textpress.htmlhelpers import script, meta, link
        from textpress.utils import dump_json
        generators = {'script': script, 'meta': meta, 'link': link,
                      'snippet': lambda html: html}
        result = [
            meta(name='generator', content='TextPress'),
            link('pingback', url_for('blog/service_rsd')),
            script(url_for('core/shared', filename='js/jQuery.js')),
            script(url_for('core/shared', filename='js/TextPress.js'))
        ]
        result.append(
            u'<script type="text/javascript">'
                u'TextPress.BLOG_URL = %s;'
            u'</script>' % dump_json(self.cfg['blog_url'].rstrip('/'))
        )
        for type, attr in _locals.page_metadata:
            result.append(generators[type](**attr))
        emit_event('before-metadata-assambled', result, buffered=True)
        return u'\n'.join(result)

    def _dispatch_request(self, environ, start_response):
        """Handle the incoming request."""
        _locals.app = self
        _locals.req = req = Request(self, environ)
        _locals.page_metadata = []

        # the after-request-setup event can return a response
        # or modify the request object in place. If we have a
        # response we just send it
        for result in emit_event('after-request-setup', req):
            if result is not None:
                return result(environ, start_response)

        # normal request dispatching
        try:
            try:
                endpoint, args = req.urls.match(req.path)
            except routing.NotFound:
                abort(404)
            except routing.RequestRedirect, e:
                redirect(e.new_url)
            else:
                resp = self.get_view(endpoint)(req, **args)
        except DirectResponse, e:
            resp = e.response

        # allow plugins to change the response object
        for result in emit_event('before-response-processed', resp):
            if result is not None:
                resp = result
                break

        return resp(environ, start_response)

    def __call__(self, environ, start_response):
        """Make the application object a WSGI application."""
        remove_app = getattr(_locals, 'app', None) is None
        try:
            for item in self.dispatch_request(environ, start_response):
                yield item
        finally:
            try:
                del _locals.req, _locals.page_metadata
                if remove_app:
                    del _locals.app
            except:
                pass


def make_app(instance_folder, bind_to_thread=False):
    """
    Creates a new instance of the application. Always use this function to
    create an application because the process of setting the application up
    requires locking which *only* happens in `make_app`.

    If bind_to_thread is true the application will be set for this thread.
    """
    _setup_lock.acquire()
    try:
        # make sure this thread has access to the variable so just set
        # up a partial class and call __init__ later.
        _locals.app = app = object.__new__(TextPress)
        app.__init__(instance_folder)
    finally:
        # if there was no error when setting up the TextPress instance
        # we should now have an attribute here to delete
        if hasattr(_locals, 'app') and not bind_to_thread:
            del _locals.app
        _setup_lock.release()

    return app
