# -*- coding: utf-8 -*-
"""
    zine.importers.wordpress
    ~~~~~~~~~~~~~~~~~~~~~~~~

    Implements an importer for WordPress extended RSS feeds.

    :copyright: (c) 2009 by the Zine Team, see AUTHORS for more details.
    :license: BSD, see LICENSE for more details.
"""
import re
from time import strptime
from datetime import datetime
from lxml import etree
from zine.forms import WordPressImportForm
from zine.importers import Importer, Blog, Tag, Category, Author, Post, Comment
from zine.i18n import lazy_gettext, _
from zine.utils import log
from zine.utils.validators import is_valid_url
from zine.utils.admin import flash
from zine.utils.xml import Namespace, html_entities, escape
from zine.utils.zeml import parse_html, inject_implicit_paragraphs
from zine.utils.http import redirect_to
from zine.utils.net import open_url
from zine.utils.text import gen_timestamped_slug
from zine.models import COMMENT_UNMODERATED, COMMENT_MODERATED, \
     STATUS_DRAFT, STATUS_PUBLISHED

CONTENT = Namespace('http://purl.org/rss/1.0/modules/content/')
DC_METADATA = Namespace('http://purl.org/dc/elements/1.1/')
WORDPRESS = Namespace('http://wordpress.org/export/1.0/')


_xml_decl_re = re.compile(r'<\?xml.*?\?>(?s)')
_meta_value_re = re.compile(r'(<wp:postmeta>.*?<wp:meta_value>)(.*?)'
                            r'(</wp:meta_value>.*?</wp:postmeta>)(?s)')
_comment_re = re.compile(r'(<wp:comment>.*?<wp:comment_content>)(.*?)'
                         r'(</wp:comment_content>.*?</wp:comment>)(?s)')
_content_encoded_re = re.compile(r'(<content:encoded>)<!\[CDATA\['
                                 r'(.*?)\]\]>(</content:encoded>)(?s)')


def _wordpress_to_html(markup):
    """Convert WordPress-HTML into read HTML."""
    return inject_implicit_paragraphs(parse_html(markup)).to_html()


def parse_broken_wxr(fd):
    """This method reads from a file descriptor and parses a WXR file as
    created by current WordPress versions.  This method also injects a
    custom DTD to not bark on HTML entities and fixes some problems with
    regular expressions before parsing.  It's not my fault, wordpress is
    that crazy :-/
    """
    # fix one: add inline doctype that defines the HTML entities so that
    # the parser doesn't bark on them, wordpress adds such entities to some
    # sections from time to time
    inline_doctype = '<!DOCTYPE wordpress [ %s ]>' % ' '.join(
        '<!ENTITY %s "&#%d;">' % (name, codepoint)
        for name, codepoint in html_entities.iteritems()
    )

    # fix two: wordpress 2.6 uses "excerpt:encoded" where excerpt is an
    # undeclared namespace.  What they did makes no sense whatsoever but
    # who cares.  We're not treating that element anyways but the XML
    # parser freaks out.  To fix that problem we're wrapping the whole
    # thing in another root element
    extra = '<wxrfix xmlns:excerpt="ignore:me">'

    code = fd.read()
    xml_decl = _xml_decl_re.search(code)
    if xml_decl is not None:
        code = code[:xml_decl.end()] + inline_doctype + extra + \
               code[xml_decl.end():]
    else:
        code = inline_doctype + extra + code

    # fix three: find comment sections and escape them.  Especially trackbacks
    # tent to break the XML structure.  same applies to wp:meta_value stuff.
    # this is especially necessary for older wordpress dumps, 2.7 fixes some
    # of these problems.
    def escape_if_good_idea(match):
        before, content, after = match.groups()
        if not content.lstrip().startswith('<![CDATA['):
            content = escape(content)
        return before + content + after
    code = _meta_value_re.sub(escape_if_good_idea, code)
    code = _comment_re.sub(escape_if_good_idea, code)
    code += '</wxrfix>'

    # fix four: WordPress uses CDATA sections for content.  Because it's very
    # likely ]]> appears in the text as literal the XML parser totally freaks
    # out there.  We've had at least one dump that does not import without
    # this hack.
    def reescape_escaped_content(match):
        before, content, after = match.groups()
        return before + escape(content) + after
    code = _content_encoded_re.sub(reescape_escaped_content, code)

    return etree.fromstring(code).find('rss').find('channel')


def parse_wordpress_date(value):
    """Parse a wordpress date or return `None` if not possible."""
    try:
        return datetime(*strptime(value, '%Y-%m-%d %H:%M:%S')[:7])
    except:
        pass


def parse_feed(fd):
    """Parse an extended WordPress RSS feed into a structure the general
    importer system can handle.  The return value is a `Blog` object.
    """
    tree = parse_broken_wxr(fd)

    authors = {}
    def get_author(name):
        if name:
            author = authors.get(name)
            if author is None:
                author = authors[name] = Author(name, None)
            return author

    tags = {}
    for item in tree.findall(WORDPRESS.tag):
        tag = Tag(item.findtext(WORDPRESS.tag_slug),
                  item.findtext(WORDPRESS.tag_name))
        tags[tag.name] = tag

    categories = {}
    for item in tree.findall(WORDPRESS.category):
        category = Category(item.findtext(WORDPRESS.category_nicename),
                            item.findtext(WORDPRESS.cat_name))
        categories[category.name] = category

    posts = []
    clean_empty_tags = re.compile("\<(?P<tag>\w+?)\>[\r\n]?\</(?P=tag)\>")

    for item in tree.findall('item'):
        status = {
            'draft':            STATUS_DRAFT
        }.get(item.findtext(WORDPRESS.status), STATUS_PUBLISHED)
        post_name = item.findtext(WORDPRESS.post_name)
        pub_date = parse_wordpress_date(item.findtext(WORDPRESS.post_date_gmt))
        content_type={'post': 'entry', 'page': 'page'}.get(
                                item.findtext(WORDPRESS.post_type), 'entry')
        slug = None

        if pub_date is None or post_name is None:
            status = STATUS_DRAFT
        if status == STATUS_PUBLISHED:
            slug = gen_timestamped_slug(post_name, content_type, pub_date)

        comments = {} # Store WordPress comment ids mapped to Comment objects
        for x in item.findall(WORDPRESS.comment):
            if x.findtext(WORDPRESS.comment_approved) != 'spam':
                commentid = x.findtext(WORDPRESS.comment_id)
                commentobj = Comment(
                    x.findtext(WORDPRESS.comment_author),
                    x.findtext(WORDPRESS.comment_content),
                    x.findtext(WORDPRESS.comment_author_email),
                    x.findtext(WORDPRESS.comment_author_url),
                    comments.get(x.findtext(WORDPRESS.comment_parent), None),
                    parse_wordpress_date(x.findtext(
                                                WORDPRESS.comment_date_gmt)),
                    x.findtext(WORDPRESS.comment_author_ip),
                    'html',
                    x.findtext(WORDPRESS.comment_type) in ('pingback',
                                                   'traceback'),
                    (COMMENT_UNMODERATED, COMMENT_MODERATED)
                    [x.findtext(WORDPRESS.comment_approved) == '1']
                    )
                comments[commentid] = commentobj

        post_body = item.findtext(CONTENT.encoded)
        post_intro = item.findtext('description')
        if post_intro == None or len(post_intro) == 0:
            find_more_results = re.split('<!--more ?.*?-->', post_body)
            if len(find_more_results) > 1:
                post_intro = clean_empty_tags.sub('',
                                       _wordpress_to_html(find_more_results[0]))
                post_body = find_more_results[1]
        post_body = clean_empty_tags.sub('', _wordpress_to_html(post_body))

        post = Post(
            slug,
            item.findtext('title'),
            item.findtext('link'),
            pub_date,
            get_author(item.findtext(DC_METADATA.creator)),
            post_intro,
            post_body,
            [tags[x.text] for x in item.findall('tag')
             if x.text in tags],
            [categories[x.text] for x in item.findall('category')
             if x.text in categories],
            comments.values(),
            item.findtext('comment_status') != 'closed',
            item.findtext('ping_status') != 'closed',
            parser='html',
            content_type=content_type
        )
        posts.append(post)

    return Blog(
        tree.findtext('title'),
        tree.findtext('link'),
        tree.findtext('description') or '',
        tree.findtext('language') or 'en',
        tags.values(),
        categories.values(),
        posts,
        authors.values()
    )


class WordPressImporter(Importer):
    name = 'wordpress'
    title = 'WordPress'

    def configure(self, request):
        form = WordPressImportForm()

        if request.method == 'POST' and form.validate(request.form):
            dump = request.files.get('dump')
            if form.data['download_url']:
                try:
                    dump = open_url(form.data['download_url']).stream
                except Exception, e:
                    error = _(u'Error downloading from URL: %s') % e
            elif not dump:
                return redirect_to('import/wordpress')

            try:
                blog = parse_feed(dump)
            except Exception, e:
                log.exception(_(u'Error parsing uploaded file'))
                flash(_(u'Error parsing uploaded file: %s') % e, 'error')
            else:
                self.enqueue_dump(blog)
                flash(_(u'Added imported items to queue.'))
                return redirect_to('admin/import')

        return self.render_admin_page('admin/import_wordpress.html',
                                      form=form.as_widget())
