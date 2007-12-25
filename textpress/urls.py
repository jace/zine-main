# -*- coding: utf-8 -*-
"""
    textpress.urls
    ~~~~~~~~~~~~~~

    This module implements a function that creates a list of urls for all
    the core components.


    :copyright: 2007 by Armin Ronacher.
    :license: GNU GPL.
"""
from werkzeug.routing import Rule, Submount


def make_urls(app):
    """Make the URLs for a new textpress application."""
    blog_urls = [
        Rule('/', defaults={'page': 1}, endpoint='blog/index'),
        Rule('/feed.atom', endpoint='blog/atom_feed'),
        Rule('/page/<int:page>', endpoint='blog/index'),
        Rule('/archive', endpoint='blog/archive'),
        Submount('/authors', [
            Rule('/', endpoint='blog/authors'),
            Rule('/<string:username>', defaults={'page': 1}, endpoint='blog/show_author'),
            Rule('/<string:username>/page/<int:page>', endpoint='blog/show_author'),
            Rule('/<string:author>/feed.atom', endpoint='blog/atom_feed'),
        ]),
        Submount('/tags', [
            Rule('/', endpoint='blog/tag_cloud'),
            Rule('/<string:slug>', defaults={'page': 1}, endpoint='blog/show_tag'),
            Rule('/<string:slug>/page/<int:page>', endpoint='blog/show_tag'),
            Rule('/<string:tag>/feed.atom', endpoint='blog/atom_feed'),
        ]),
        Rule('/_services/', endpoint='blog/service_rsd'),
        Rule('/_services/json/<path:identifier>', endpoint='blog/json_service'),
        Rule('/_services/xml/<path:identifier>', endpoint='blog/xml_service')
    ]
    admin_urls = [
        Rule('/', endpoint='admin/index'),
        Rule('/login', endpoint='admin/login'),
        Rule('/logout', endpoint='admin/logout'),
        Rule('/posts/', endpoint='admin/show_posts'),
        Rule('/posts/new', endpoint='admin/new_post'),
        Rule('/posts/<int:post_id>', endpoint='admin/edit_post'),
        Rule('/posts/<int:post_id>/delete', endpoint='admin/delete_post'),
        Rule('/posts/<int:post_id>/comments', endpoint='admin/show_comments'),
        Rule('/comments/', endpoint='admin/show_comments'),
        Rule('/comments/<int:comment_id>', endpoint='admin/edit_comment'),
        Rule('/comments/<int:comment_id>/delete', endpoint='admin/delete_comment'),
        Rule('/comments/<int:comment_id>/unblock', endpoint='admin/unblock_comment'),
        Rule('/tags/', endpoint='admin/show_tags'),
        Rule('/tags/new', endpoint='admin/new_tag'),
        Rule('/tags/<int:tag_id>', endpoint='admin/edit_tag'),
        Rule('/tags/<int:tag_id>/delete', endpoint='admin/delete_tag'),
        Rule('/users/', endpoint='admin/show_users'),
        Rule('/users/new', endpoint='admin/new_user'),
        Rule('/users/<int:user_id>', endpoint='admin/edit_user'),
        Rule('/users/<int:user_id>/delete', endpoint='admin/delete_user'),
        Rule('/options/', endpoint='admin/options'),
        Rule('/options/basic', endpoint='admin/basic_options'),
        Rule('/options/theme/', endpoint='admin/theme'),
        Rule('/options/theme/overlays/', endpoint='admin/overlays'),
        Rule('/options/theme/overlays/<path:template>',
             endpoint='admin/overlays'),
        Rule('/options/widgets', endpoint='admin/widgets'),
        Rule('/options/plugins/', endpoint='admin/plugins'),
        Rule('/options/plugins/<plugin>/remove', endpoint='admin/remove_plugin'),
        Rule('/options/configuration', endpoint='admin/configuration'),
        Rule('/about/', endpoint='admin/about'),
        Rule('/about/eventmap', endpoint='admin/eventmap'),
        Rule('/about/textpress', endpoint='admin/about_textpress'),
        Rule('/change_password', endpoint='admin/change_password')
    ]

    # add the more complex url rule for archive and show post
    tmp = '/'
    for digits, part in [(4, 'year'), (2, 'month'), (2, 'day')]:
        tmp += '<int(fixed_digits=%d):%s>/' % (digits, part)
        blog_urls.extend([
            Rule(tmp, defaults={'page': 1}, endpoint='blog/archive'),
            Rule(tmp + 'page/<int:page>', endpoint='blog/archive'),
            Rule(tmp + 'feed.atom', endpoint='blog/atom_feed')
        ])
    blog_urls.extend([
        Rule(tmp + '<slug>', endpoint='blog/show_post'),
        Rule(tmp + '<post_slug>/feed.atom', endpoint='blog/atom_feed')
    ])

    return [
        Submount(app.cfg['blog_url_prefix'], blog_urls),
        Submount('/admin', admin_urls)
    ]
