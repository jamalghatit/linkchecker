# Copyright (C) 2000-2014 Bastian Kleineidam
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
"""
Base URL handler.
"""
# pylint: disable=assignment-from-none, catching-non-exception, no-member

import sys
import os
import urllib.parse
from urllib.request import urlopen
import time
import errno
import socket
from io import BytesIO

from . import absolute_url, get_url_from
from .. import (
    log,
    LOG_CHECK,
    strformat,
    LinkCheckerError,
    url as urlutil,
    trace,
    get_link_pat,
)
from ..htmlutil import htmlsoup
from ..network import iputil
from .const import (
    WARN_URL_EFFECTIVE_URL,
    WARN_URL_ERROR_GETTING_CONTENT,
    WARN_URL_OBFUSCATED_IP,
    WARN_URL_CONTENT_SIZE_ZERO,
    WARN_URL_CONTENT_SIZE_TOO_LARGE,
    WARN_URL_CONTENT_TYPE_UNPARSEABLE,
    WARN_URL_WHITESPACE,
    URL_MAX_LENGTH,
    WARN_URL_TOO_LONG,
    ExcList,
    ExcSyntaxList,
    ExcNoCacheList,
)
from ..url import url_fix_wayback_query

# schemes that are invalid with an empty hostname
scheme_requires_host = ("ftp", "http")


def urljoin(parent, url):
    """
    If url is relative, join parent and url. Else leave url as-is.

    @return: joined url
    """
    if urlutil.url_is_absolute(url):
        return url
    return urllib.parse.urljoin(parent, url)


def url_norm(url, encoding):
    """Wrapper for url.url_norm() to convert UnicodeError in
    LinkCheckerError."""
    try:
        return urlutil.url_norm(url, encoding=encoding)
    except UnicodeError:
        msg = _("URL has unparsable domain name: %(name)s") % {
            "name": sys.exc_info()[1]
        }
        raise LinkCheckerError(msg)


class UrlBase:
    """An URL with additional information like validity etc."""

    # file types that can be parsed recursively
    ContentMimetypes = {
        "text/html": "html",
        "application/xhtml+xml": "html",
        # Include PHP file which helps when checking local .php files.
        # It does not harm other URL schemes like HTTP since HTTP servers
        # should not send this content type. They send text/html instead.
        "application/x-httpd-php": "html",
        "text/css": "css",
        "application/vnd.adobe.flash.movie": "swf",
        "application/x-shockwave-flash": "swf",
        "application/msword": "word",
        "text/plain+linkchecker": "text",
        "text/plain+opera": "opera",
        "text/plain+chromium": "chromium",
        "application/x-plist+safari": "safari",
        "text/vnd.wap.wml": "wml",
        "application/xml+sitemap": "sitemap",
        "application/xml+sitemapindex": "sitemapindex",
        "application/pdf": "pdf",
        "application/x-pdf": "pdf",
    }

    # Read in 16kb chunks
    ReadChunkBytes = 1024 * 16

    def __init__(
        self,
        base_url,
        recursion_level,
        aggregate,
        parent_url=None,
        base_ref=None,
        line=-1,
        column=-1,
        page=-1,
        name="",
        url_encoding=None,
        extern=None,
    ):
        """
        Initialize check data, and store given variables.

        @param base_url: unquoted and possibly unnormed url
        @param recursion_level: on what check level lies the base url
        @param aggregate: aggregate instance
        @param parent_url: quoted and normed url of parent or None
        @param base_ref: quoted and normed url of <base href=""> or None
        @param line: line number of url in parent content
        @param column: column number of url in parent content
        @param page: page number of url in parent content
        @param name: name of url or empty
        @param url_encoding: encoding of URL or None
        @param extern: None or (is_extern, is_strict)
        """
        self.reset()
        self.init(
            base_ref,
            base_url,
            parent_url,
            recursion_level,
            aggregate,
            line,
            column,
            page,
            name,
            url_encoding,
            extern,
        )
        self.check_syntax()
        if recursion_level == 0:
            self.add_intern_pattern()
        self.set_extern(self.url)
        if self.extern[0] and self.extern[1]:
            self.add_info(
                _("The URL is outside of the domain filter, checked only syntax.")
            )
            if not self.has_result:
                self.set_result(_("filtered"))

    def init(
        self,
        base_ref,
        base_url,
        parent_url,
        recursion_level,
        aggregate,
        line,
        column,
        page,
        name,
        url_encoding,
        extern,
    ):
        """
        Initialize internal data.
        """
        self.base_ref = base_ref
        if self.base_ref is not None:
            assert isinstance(self.base_ref, str), repr(self.base_ref)
        self.base_url = base_url.strip() if base_url else base_url
        if self.base_url is not None:
            assert isinstance(self.base_url, str), repr(self.base_url)
        self.parent_url = parent_url
        if self.parent_url is not None:
            assert isinstance(self.parent_url, str), repr(self.parent_url)
        self.recursion_level = recursion_level
        self.aggregate = aggregate
        self.line = line
        self.column = column
        self.page = page
        self.name = name
        assert isinstance(self.name, str), repr(self.name)
        self.encoding = url_encoding
        self.extern = extern
        if self.base_ref:
            assert not urlutil.url_needs_quoting(self.base_ref), (
                "unquoted base reference URL %r" % self.base_ref
            )
        if self.parent_url:
            assert not urlutil.url_needs_quoting(self.parent_url), (
                "unquoted parent URL %r" % self.parent_url
            )
        url = absolute_url(self.base_url, base_ref, parent_url)
        # assume file link if no scheme is found
        self.scheme = url.split(":", 1)[0].lower() or "file"
        if self.base_url != base_url:
            self.add_warning(
                _("Leading or trailing whitespace in URL `%(url)s'.")
                % {"url": base_url},
                tag=WARN_URL_WHITESPACE,
            )
        self.ignore_errors = self.aggregate.config['ignoreerrors']

    def reset(self):
        """
        Reset all variables to default values.
        """
        # self.url is constructed by self.build_url() out of base_url
        # and (base_ref or parent) as absolute and normed url.
        # This the real url we use when checking so it also referred to
        # as 'real url'
        self.url = None
        # a split version of url for convenience
        self.urlparts = None
        # the scheme, host, port and anchor part of url
        self.scheme = self.host = self.port = self.anchor = None
        # the result message string and flag
        self.result = ""
        self.has_result = False
        # valid or not
        self.valid = True
        # list of warnings (without duplicates)
        self.warnings = []
        # list of infos
        self.info = []
        # content size
        self.size = -1
        # last modification time of content in HTTP-date format
        # as specified in RFC2616 chapter 3.3.1
        self.modified = None
        # download time
        self.dltime = -1
        # check time
        self.checktime = 0
        # connection object
        self.url_connection = None
        # data of url content,  (data == None) means no data is available
        self.data = None
        # url content data encoding
        self.content_encoding = None
        # url content as a Unicode string
        self.text = None
        # url content as a Beautiful Soup object
        self.soup = None
        # cache url is set by build_url() calling set_cache_url()
        self.cache_url = None
        # extern flags (is_extern, is_strict)
        self.extern = None
        # flag if the result should be cached
        self.caching = True
        # title is either the URL or parsed from content
        self.title = None
        # flag if content should be checked or not
        self.do_check_content = True
        # MIME content type
        self.content_type = ""
        # URLs seen through redirections
        self.aliases = []
        # error messages (regular expressions) to ignore
        self.ignore_errors = []

    def set_result(self, msg, valid=True, overwrite=False):
        """
        Set result string and validity.
        """
        if self.has_result and not overwrite:
            log.warn(
                LOG_CHECK,
                "Double result %r (previous %r) for %s",
                msg,
                self.result,
                self,
            )
        else:
            self.has_result = True
        if not msg:
            log.warn(LOG_CHECK, "Empty result for %s", self)
        self.result = msg
        self.valid = valid

        if not self.valid:
            for url_regex, msg_regex in self.ignore_errors:
                if not url_regex.search(self.url):
                    continue
                if not msg_regex.search(self.result):
                    continue
                self.valid = True
                self.result = f"Ignored: {self.result}"

        # free content data
        self.data = None

    def get_title(self):
        """Return title of page the URL refers to.
        This is per default the filename or the URL."""
        if self.title is None:
            url = ""
            if self.base_url:
                url = self.base_url
            elif self.url:
                url = self.url
            self.title = url
            if "/" in url:
                title = url.rsplit("/", 1)[1]
                if title:
                    self.title = title
        return self.title

    def is_content_type_parseable(self):
        """
        Return True iff the content type of this url is parseable.
        """
        if self.content_type in self.ContentMimetypes:
            return True
        log.debug(
            LOG_CHECK,
            "URL with content type %r is not parseable",
            self.content_type,
        )
        if self.recursion_level == 0:
            self.add_warning(
                _("The URL with content type %r is not parseable.") % self.content_type,
                tag=WARN_URL_CONTENT_TYPE_UNPARSEABLE,
            )
        return False

    def is_parseable(self):
        """
        Return True iff content of this url is parseable.
        """
        return False

    def is_html(self):
        """Return True iff content of this url is HTML formatted."""
        return self._is_ctype("html")

    def is_css(self):
        """Return True iff content of this url is CSS stylesheet."""
        return self._is_ctype("css")

    def _is_ctype(self, ctype):
        """Return True iff content is valid and of the given type."""
        if not self.valid:
            return False
        mime = self.content_type
        return self.ContentMimetypes.get(mime) == ctype

    def is_http(self):
        """Return True for *http://* or *https://* URLs."""
        return self.scheme in ("http", "https")

    def is_file(self):
        """Return True for *file://* URLs."""
        return self.scheme == "file"

    def is_directory(self):
        """Return True if current URL represents a directory."""
        return False

    def is_local(self):
        """Return True for local (ie. *file://*) URLs."""
        return self.is_file()

    def add_warning(self, s, tag=None):
        """
        Add a warning string.
        """
        item = (tag, s)
        if item not in self.warnings:
            if tag in self.aggregate.config["ignorewarnings"]:
                self.add_info(s)
            else:
                self.warnings.append(item)

    def add_info(self, s):
        """
        Add an info string.
        """
        if s not in self.info:
            self.info.append(s)

    def set_cache_url(self):
        """Set the URL to be used for caching."""
        if "AnchorCheck" in self.aggregate.config["enabledplugins"]:
            self.cache_url = self.url
        else:
            # remove anchor from cached target url since we assume
            # URLs with different anchors to have the same content
            self.cache_url = urlutil.urlunsplit(self.urlparts[:4] + [''])
        log.debug(LOG_CHECK, "cache_url '%s'", self.cache_url)

    def check_syntax(self):
        """
        Called before self.check(), this function inspects the
        url syntax. Success enables further checking, failure
        immediately logs this url. Syntax checks must not
        use any network resources.
        """
        log.debug(LOG_CHECK, "checking syntax")
        if self.base_url is None:
            self.base_url = ""
        if not (self.base_url or self.parent_url):
            self.set_result(_("URL is empty"), valid=False)
            return
        try:
            self.build_url()
            self.check_url_warnings()
        except tuple(ExcSyntaxList) as msg:
            self.set_result(str(msg), valid=False)
        else:
            self.set_cache_url()

    def check_url_warnings(self):
        """Check URL name and length."""
        effectiveurl = urlutil.urlunsplit(self.urlparts)
        if self.url != effectiveurl:
            self.add_warning(
                _("Effective URL %(url)r.") % {"url": effectiveurl},
                tag=WARN_URL_EFFECTIVE_URL,
            )
            self.url = effectiveurl
        if len(self.url) > URL_MAX_LENGTH and self.scheme != "data":
            args = dict(len=len(self.url), max=URL_MAX_LENGTH)
            self.add_warning(
                _("URL length %(len)d is longer than %(max)d.") % args,
                tag=WARN_URL_TOO_LONG,
            )

    def build_url(self):
        """
        Construct self.url and self.urlparts out of the given base
        url information self.base_url, self.parent_url and self.base_ref.
        """
        # norm base url - can raise UnicodeError from url.idna_encode()
        base_url, is_idn = url_norm(self.base_url, self.encoding)
        # make url absolute
        if self.base_ref:
            # use base reference as parent url
            if ":" not in self.base_ref:
                # some websites have a relative base reference
                self.base_ref = urljoin(self.parent_url, self.base_ref)
            self.url = urljoin(self.base_ref, base_url)
        elif self.parent_url:
            # strip the parent url anchor
            urlparts = list(urllib.parse.urlsplit(self.parent_url))
            urlparts[4] = ""
            parent_url = urlutil.urlunsplit(urlparts)
            self.url = urljoin(parent_url, base_url)
        else:
            self.url = base_url
        # urljoin can unnorm the url path, so norm it again
        urlparts = list(urllib.parse.urlsplit(self.url))
        if urlparts[2]:
            urlparts[2] = urlutil.collapse_segments(urlparts[2])
            if not urlparts[0].startswith("feed"):
                # restore second / in http[s]:// in wayback path
                urlparts[2] = url_fix_wayback_query(urlparts[2])
        self.url = urlutil.urlunsplit(urlparts)
        self.urlparts = self.build_url_parts(self.url)
        # and unsplit again
        self.url = urlutil.urlunsplit(self.urlparts)

    def build_url_parts(self, url):
        """Set userinfo, host, port and anchor from url and return urlparts.
        Also checks for obfuscated IP addresses.
        """
        split = urllib.parse.urlsplit(url)
        urlparts = list(split)
        # check userinfo@host:port syntax
        self.userinfo, host = urlutil.split_netloc(split.netloc)
        try:
            port = split.port
        except ValueError:
            raise LinkCheckerError(
                _("URL host %(host)r has invalid port") % {"host": host}
            )
        if port is None:
            port = urlutil.default_ports.get(self.scheme, 0)
        if port is None:
            raise LinkCheckerError(
                _("URL host %(host)r has invalid port") % {"host": host}
            )
        self.port = port
        # urllib.parse.SplitResult.hostname is lowercase
        self.host = split.hostname
        if self.scheme in scheme_requires_host:
            if not self.host:
                raise LinkCheckerError(_("URL has empty hostname"))
            self.check_obfuscated_ip()
        if not self.port or self.port == urlutil.default_ports.get(self.scheme):
            host = self.host
        else:
            host = f"{self.host}:{self.port}"
        if self.userinfo:
            urlparts[1] = f"{self.userinfo}@{host}"
        else:
            urlparts[1] = host
        # save anchor for later checking
        self.anchor = split.fragment
        if self.anchor is not None:
            assert isinstance(self.anchor, str), repr(self.anchor)
        return urlparts

    def check_obfuscated_ip(self):
        """Warn if host of this URL is obfuscated IP address."""
        # check if self.host can be an IP address
        # check for obfuscated IP address
        if iputil.is_obfuscated_ip(self.host):
            ips = iputil.resolve_host(self.host)
            if ips:
                self.host = ips[0]
                self.add_warning(
                    _("URL %(url)s has obfuscated IP address %(ip)s")
                    % {"url": self.base_url, "ip": ips[0]},
                    tag=WARN_URL_OBFUSCATED_IP,
                )

    def check(self):
        """Main check function for checking this URL."""
        if self.aggregate.config["trace"]:
            trace.trace_on()
        try:
            self.local_check()
        except OSError:
            # on Unix, ctrl-c can raise
            # error: (4, 'Interrupted system call')
            etype, value = sys.exc_info()[:2]
            if etype == errno.EINTR:
                raise KeyboardInterrupt(value)
            else:
                raise

    def local_check(self):
        """Local check function can be overridden in subclasses."""
        log.debug(LOG_CHECK, "Checking %s", self)
        # strict extern URLs should not be checked
        assert not self.extern[1], 'checking strict extern URL'
        # check connection
        log.debug(LOG_CHECK, "checking connection")
        try:
            self.check_connection()
            self.set_content_type()
            self.add_size_info()
            self.aggregate.plugin_manager.run_connection_plugins(self)
        except tuple(ExcList) as exc:
            value = self.handle_exception()
            # make nicer error msg for unknown hosts
            if isinstance(exc, socket.error) and exc.args[0] == -2:
                value = _('Hostname not found')
            elif isinstance(exc, UnicodeError):
                # idna.encode(host) failed
                value = _('Bad hostname %(host)r: %(msg)s') % {
                    'host': self.host,
                    'msg': value,
                }
            self.set_result(value, valid=False)

    def check_content(self):
        """Check content of URL.
        @return: True if content can be parsed, else False
        """
        if self.do_check_content and self.valid:
            # check content and recursion
            try:
                if self.can_get_content():
                    self.aggregate.plugin_manager.run_content_plugins(self)
                if self.allows_recursion():
                    return True
            except tuple(ExcList):
                value = self.handle_exception()
                self.add_warning(
                    _("could not get content: %(msg)s") % {"msg": value},
                    tag=WARN_URL_ERROR_GETTING_CONTENT,
                )
        return False

    def close_connection(self):
        """
        Close an opened url connection.
        """
        if self.url_connection is None:
            # no connection is open
            return
        try:
            self.url_connection.close()
        except Exception:
            # ignore close errors
            pass
        self.url_connection = None

    def handle_exception(self):
        """
        An exception occurred. Log it and set the cache flag.
        """
        etype, evalue = sys.exc_info()[:2]
        log.debug(
            LOG_CHECK, "Error in %s: %s %s", self.url, etype, evalue, exception=True
        )
        # note: etype must be the exact class, not a subclass
        if (
            (etype in ExcNoCacheList)
            or (etype == socket.error and evalue.args[0] == errno.EBADF)
            or not evalue
        ):
            # EBADF occurs when operating on an already socket
            self.caching = False
        # format message "<exception name>: <error message>"
        errmsg = etype.__name__
        if evalue:
            errmsg += f": {evalue}"
        # limit length to 240
        return strformat.limit(errmsg, length=240)

    def check_connection(self):
        """
        The basic connection check uses urlopen to initialize
        a connection object.
        """
        self.url_connection = urlopen(self.url)

    def add_size_info(self):
        """Set size of URL content (if any)..
        Should be overridden in subclasses."""
        maxbytes = self.aggregate.config["maxfilesizedownload"]
        if self.size > maxbytes:
            self.add_warning(
                _("Content size %(size)s is larger than %(maxbytes)s.")
                % dict(
                    size=strformat.strsize(self.size),
                    maxbytes=strformat.strsize(maxbytes),
                ),
                tag=WARN_URL_CONTENT_SIZE_TOO_LARGE,
            )

    def allows_simple_recursion(self):
        """Check recursion level and extern status."""
        rec_level = self.aggregate.config["recursionlevel"]
        if rec_level >= 0 and self.recursion_level >= rec_level:
            log.debug(LOG_CHECK, "... no, maximum recursion level reached.")
            return False
        if self.extern[0]:
            log.debug(LOG_CHECK, "... no, extern.")
            return False
        return True

    def allows_recursion(self):
        """
        Return True iff we can recurse into the url's content.
        """
        log.debug(LOG_CHECK, "checking recursion of %r ...", self.url)
        if not self.valid:
            log.debug(LOG_CHECK, "... no, invalid.")
            return False
        if not self.can_get_content():
            log.debug(LOG_CHECK, "... no, cannot get content.")
            return False
        if not self.allows_simple_recursion():
            return False
        if self.size > self.aggregate.config["maxfilesizeparse"]:
            log.debug(LOG_CHECK, "... no, maximum parse size.")
            return False
        if not self.is_parseable():
            log.debug(LOG_CHECK, "... no, not parseable.")
            return False
        if not self.content_allows_robots():
            log.debug(LOG_CHECK, "... no, robots.")
            return False
        log.debug(LOG_CHECK, "... yes, recursion.")
        return True

    def content_allows_robots(self):
        """Returns True: only check robots.txt on HTTP links."""
        return True

    def set_extern(self, url):
        """
        Match URL against extern and intern link patterns. If no pattern
        matches the URL is extern. Sets self.extern to a tuple (bool,
        bool) with content (is_extern, is_strict).

        @return: None
        """
        if self.extern:
            return
        if not url:
            self.extern = (1, 1)
            return
        for entry in self.aggregate.config["externlinks"]:
            match = entry['pattern'].search(url)
            if (entry['negate'] and not match) or (match and not entry['negate']):
                log.debug(LOG_CHECK, "Extern URL %r", url)
                self.extern = (1, entry['strict'])
                return
        for entry in self.aggregate.config["internlinks"]:
            match = entry['pattern'].search(url)
            if (entry['negate'] and not match) or (match and not entry['negate']):
                log.debug(LOG_CHECK, "Intern URL %r", url)
                self.extern = (0, 0)
                return
        if self.aggregate.config['checkextern']:
            self.extern = (1, 0)
        else:
            self.extern = (1, 1)

    def set_content_type(self):
        """Set content MIME type.
        Should be overridden in subclasses."""
        pass

    def can_get_content(self):
        """Indicate whether url get_content() can be called."""
        return self.size <= self.aggregate.config["maxfilesizedownload"]

    def download_content(self):
        log.debug(LOG_CHECK, "Get content of %r", self.url)
        t = time.time()
        content = self.read_content()
        self.size = len(content)
        self.dltime = time.time() - t
        if self.size == 0:
            self.add_warning(_("Content size is zero."), tag=WARN_URL_CONTENT_SIZE_ZERO)
        else:
            self.aggregate.add_downloaded_bytes(self.size)
        return content

    def get_soup(self):
        if self.soup is None:
            self.get_content()
        return self.soup

    def get_raw_content(self):
        if self.data is None:
            self.data = self.download_content()
        return self.data

    def get_content(self, encoding=None):
        if self.text is None:
            self.get_raw_content()
            self.soup = htmlsoup.make_soup(self.data, encoding)
            # Sometimes soup.original_encoding is None!  Better mangled text
            # than an internal crash, eh?  ISO-8859-1 is a safe fallback in the
            # sense that any binary blob can be decoded, it'll never cause a
            # UnicodeDecodeError.
            log.debug(
                LOG_CHECK, "Beautiful Soup detected %s", self.soup.original_encoding
            )
            self.content_encoding = self.soup.original_encoding or 'ISO-8859-1'
            log.debug(LOG_CHECK, "Content encoding %s", self.content_encoding)
            self.text = self.data.decode(self.content_encoding)
        return self.text

    def read_content(self):
        """Return data for this URL. Can be overridden in subclasses."""
        buf = BytesIO()
        data = self.read_content_chunk()
        while data:
            if buf.tell() + len(data) > self.aggregate.config["maxfilesizedownload"]:
                raise LinkCheckerError(_("File size too large"))
            buf.write(data)
            data = self.read_content_chunk()
        return buf.getvalue()

    def read_content_chunk(self):
        """Read one chunk of content from this URL.
        Precondition: url_connection is an opened URL.
        """
        return self.url_connection.read(self.ReadChunkBytes)

    def get_user_password(self):
        """Get tuple (user, password) from configured authentication.
        Both user and password can be None.
        """
        if self.userinfo:
            # URL itself has authentication info
            split = urllib.parse.urlsplit(self.url)
            return (split.username, split.password)
        return self.aggregate.config.get_user_password(self.url)

    def add_url(self, url, line=0, column=0, page=0, name="", base=None, parent=None):
        """Add new URL to queue."""
        if base:
            base_ref = urlutil.url_norm(base, encoding=self.content_encoding)[0]
        else:
            base_ref = None
        url_data = get_url_from(
            url,
            self.recursion_level + 1,
            self.aggregate,
            parent_url=self.url if parent is None else parent,
            base_ref=base_ref,
            line=line,
            column=column,
            page=page,
            name=name,
            parent_content_type=self.content_type,
            url_encoding=self.content_encoding,
        )
        self.aggregate.urlqueue.put(url_data)

    def serialized(self, sep=os.linesep):
        """
        Return serialized url check data as unicode string.
        """
        return sep.join(
            [
                "%s link" % self.scheme,
                "base_url=%r" % self.base_url,
                "parent_url=%r" % self.parent_url,
                "base_ref=%r" % self.base_ref,
                "recursion_level=%d" % self.recursion_level,
                "url_connection=%s" % self.url_connection,
                "line=%s" % self.line,
                "column=%s" % self.column,
                "page=%d" % self.page,
                "name=%r" % self.name,
                "anchor=%r" % self.anchor,
                "cache_url=%s" % self.cache_url,
            ]
        )

    def get_intern_pattern(self, url=None):
        """Get pattern for intern URL matching.

        @param url: the URL to set intern pattern for, else self.url
        @type url: unicode or None
        @return: non-empty regex pattern or None
        @rtype: String or None
        """
        return None

    def add_intern_pattern(self, url=None):
        """Add intern URL regex to config."""
        try:
            pat = self.get_intern_pattern(url=url)
            if pat:
                log.debug(LOG_CHECK, "Add intern pattern %r", pat)
                self.aggregate.config['internlinks'].append(get_link_pat(pat))
        except UnicodeError as msg:
            res = _("URL has unparsable domain name: %(domain)s") % {"domain": msg}
            self.set_result(res, valid=False)

    def __str__(self):
        """
        Get URL info.

        @return: URL info
        @rtype: unicode
        """
        return self.serialized()

    def __bytes__(self):
        """
        Get URL info.

        @return: URL info, encoded with the output logger encoding
        @rtype: string
        """
        s = str(self)
        return self.aggregate.config['logger'].encode(s)

    def __repr__(self):
        """
        Get URL info.

        @return: URL info
        @rtype: unicode
        """
        return f'<{self.serialized(sep=", ")}>'

    def to_wire_dict(self):
        """Return a simplified transport object for logging and caching.

        The transport object must contain these attributes:

        - url_data.valid: bool
          Indicates if URL is valid
        - url_data.result: unicode
          Result string
        - url_data.warnings: list of tuples (tag, warning message)
          List of tagged warnings for this URL.
        - url_data.name: unicode string or None
          name of URL (eg. filename or link name)
        - url_data.parent_url: unicode or None
          Parent URL
        - url_data.base_ref: unicode
          HTML base reference URL of parent
        - url_data.url: unicode
          Fully qualified URL.
        - url_data.domain: unicode
          URL domain part.
        - url_data.checktime: int
          Number of seconds needed to check this link, default: zero.
        - url_data.dltime: int
          Number of seconds needed to download URL content, default: -1
        - url_data.size: int
          Size of downloaded URL content, default: -1
        - url_data.info: list of unicode
          Additional information about this URL.
        - url_data.line: int
          Line number of this URL at parent document, or None
        - url_data.column: int
          Column number of this URL at parent document, or None
        - url_data.page: int
          Page number of this URL at parent document, or -1
        - url_data.cache_url: unicode
          Cache url for this URL.
        - url_data.content_type: unicode
          MIME content type for URL content.
        - url_data.level: int
          Recursion level until reaching this URL from start URL
        - url_data.last_modified: datetime
          Last modification date of retrieved page (or None).
        """
        return dict(
            valid=self.valid,
            extern=self.extern[0],
            result=self.result,
            warnings=self.warnings[:],
            name=self.name or "",
            title=self.get_title(),
            parent_url=self.parent_url or "",
            base_ref=self.base_ref or "",
            base_url=self.base_url or "",
            url=self.url or "",
            domain=(self.urlparts[1] if self.urlparts else ""),
            checktime=self.checktime,
            dltime=self.dltime,
            size=self.size,
            info=self.info,
            line=self.line,
            column=self.column,
            page=self.page,
            cache_url=self.cache_url,
            content_type=self.content_type,
            level=self.recursion_level,
            modified=self.modified,
        )

    def to_wire(self):
        """Return compact UrlData object with information from to_wire_dict().
        """
        return CompactUrlData(self.to_wire_dict())


urlDataAttr = [
    'valid',
    'extern',
    'result',
    'warnings',
    'name',
    'title',
    'parent_url',
    'base_ref',
    'base_url',
    'url',
    'domain',
    'checktime',
    'dltime',
    'size',
    'info',
    'modified',
    'line',
    'column',
    'page',
    'cache_url',
    'content_type',
    'level',
]


class CompactUrlData:
    """Store selected UrlData attributes in slots to minimize memory usage."""

    __slots__ = urlDataAttr

    def __init__(self, wired_url_data):
        '''Set all attributes according to the dictionary wired_url_data'''
        for attr in urlDataAttr:
            setattr(self, attr, wired_url_data[attr])
