# -*- coding: utf-8 -*-
"""
    zine.widgets
    ~~~~~~~~~~~~

    This module provides the core widgets and functionality to build your
    own.  Widgets are, in the context of Zine, classes that have a
    unicode conversion function that renders a template into a string but
    have all their attributes attached to themselves.  This gives template
    designers the ability to change the general widget template but also
    render one widget differently.

    Additionally widgets could be moved around from the admin panel in the
    future.

    :copyright: Copyright 2007-2008 by Armin Ronacher, Pedro Algarvio,
                                       Christopher Grebs, Ali Afshar.
    :license: GNU GPL.
"""
from zine.application import render_template
from zine.models import Post, Category, Comment


class Widget(object):
    """Baseclass for all the widgets out there!"""

    #: the name of the widget when called from a template.  This is also used
    #: if widgets are configured from the admin panel to have a unique
    #: identifier.
    name = None

    #: name of the template for this widget. Please prefix those template
    #: names with an underscore to mark it as partial. The widget is available
    #: in the template as `widget`.
    template = None

    def __unicode__(self):
        """Render the template."""
        return render_template(self.template, widget=self)

    def __str__(self):
        return unicode(self).encode('utf-8')


class PostArchiveSummary(Widget):
    """Show the last n months/years/days with posts."""

    name = 'post_archive_summary'
    template = 'widgets/post_archive_summary.html'

    def __init__(self, detail='months', limit=6, show_title=False):
        self.__dict__.update(Post.query.get_archive_summary(detail, limit))
        self.show_title = show_title


class LatestPosts(Widget):
    """Show the latest n posts."""

    name = 'latest_posts'
    template = 'widgets/latest_posts.html'

    def __init__(self, limit=5, show_title=False, content_types=None):
        if content_types is None:
            query = Post.query.for_index()
        else:
            query = Post.query.filter(Post.content_type.in_(content_types))
        self.posts = query.latest(limit).all()
        self.show_title = show_title


class LatestComments(Widget):
    """Show the latest n comments."""

    name = 'latest_comments'
    template = 'widgets/latest_comments.html'

    def __init__(self, limit=5, show_title=False, ignore_blocked=False):
        self.comments = Comment.query. \
            latest(limit, ignore_blocked=ignore_blocked).all()
        self.show_title = show_title


#: list of all core widgets
all_widgets = [PostArchiveSummary, LatestPosts, LatestComments]
