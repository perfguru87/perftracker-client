#!/usr/bin/env python

from __future__ import print_function

# -*- coding: utf-8 -*-
__author__ = "perfguru87@gmail.com"
__copyright__ = "Copyright 2018, The PerfTracker project"
__license__ = "MIT"

"""
A browser Page helper
"""

from utils import parse_url
import time
import copy
import logging
import sys
import os
import pickle
import base64
from collections import defaultdict

sys.path.append(os.path.join(os.path.split(os.path.abspath(__file__))[0], ".."))
from browser.utils import get_common_url_prefix
from helpers.texttable import TextTable


class PageTimeline:
    types = ['navStrt', 'reqStrt', 'rspStrt', 'rspEnd', 'domEnd', 'onloadEnd', 'ajaxEnd']
    jstypes = {'navStrt': 'navigationStart',
               'reqStrt': 'requestStart',
               'rspStrt': 'responseStart',
               'rspEnd': 'responseEnd',
               'domEnd': 'domComplete',
               'onloadEnd': 'loadEventEnd'
               }

    def __init__(self, page, values=None):
        self.page = page
        self.values = {}
        self.deltas = []
        self.ajax_start = 0
        self.total_dur = 0

        for t in self.types:
            self.values[t] = values[t] if values and t in values else 0

        for n in range(0, len(self.types) - 1):
            self.deltas.append(0)

        if page.ts_end and not self.values['ajaxEnd']:
            self.values['ajaxEnd'] = page.ts_end - page.ts_start + self.values['navStrt']

        # fixup

        if self.values['ajaxEnd'] < self.values['onloadEnd']:
            self.values['ajaxEnd'] = self.values['onloadEnd']

        for n in range(0, len(self.types) - 1):
            self.deltas[n] = self.values[self.types[n + 1]] - self.values[self.types[n]]

        self.ajax_start = self.values['domEnd'] - self.values['navStrt']
        self.total_dur = self.values['onloadEnd'] - self.values['navStrt']


class PageEvent:
    def __init__(self, name, params):
        self.name = name
        if type(params) == dict:
            self.params = params
        else:
            try:
                self.params = json.loads(args)
            except ValueError:
                logging.warning("Can't convert event '%s' parameters '%s' to Json" % (name, args))


class PageRequestsGroup:
    def __init__(self, request, threshold_ms=10):
        self.threshold_ms = threshold_ms
        self.ts_start = request.ts_start
        self.ts_end = request.ts_start + request.dur if request.ts_start else None
        self.requests = [request]

    def _add(self, request):
        if self.ts_start is None:
            self.ts_start = 0
        if self.ts_end is None:
            self.ts_end = 0
        self.ts_start = min(self.ts_start, request.ts_start) if self.ts_start else None
        self.ts_end = max(self.ts_end, request.ts_start + request.dur) if self.ts_end else None
        self.requests.append(request)

    def add_request(self, request):
        if not self.ts_start:
            self._add(request)
            return True
        if self.ts_start <= request.ts_start and request.ts_start <= self.ts_end + self.threshold_ms:
            self._add(request)
            return True
        return False

    def get_uncached_reqs(self):
        return [r for r in self.requests if not r.cached]


_page_req_id = 0


class PageRequest:
    types = ["Image", "Stylesheet", "Script", "XHR", "Document", "Other"]
    doc_types = ["Document", "Other"]
    types_abbr = {"Image": "IMG", "Stylesheet": "CSS", "Script": "JS", "XHR": "XHR",
                  "Document": "Doc", "Other": "Oth", "Total": "Tot"}

    def __init__(self, page, id=None):
        global _page_req_id
        _page_req_id += 1

        self.page = page
        self.id = id if id else _page_req_id
        self.method = None
        self.url = None
        self.ts_start = None
        self.ts_end = None
        self.content_length = 0
        self.length = 0
        self.dur = 0
        self.connection_reused = False
        self.status = None
        self.type = 'Other'
        self.keepalive = False
        self.gzipped = False
        self.cached = False
        self.completed = False

        self.data = ""
        self.page_actions = None

        # properties below needed to re-execute request
        self.params = None
        self.header = {}
        self.validator = None
        self.valid_statuses = None

        self.longpoll = False

    def __str__(self):
        return str(self.url)

    def __unicode__(self):
        return str(self)

    def duplicate(self):
        result = copy.deepcopy(self)

        global _page_req_id
        _page_req_id += 1
        result.id = _page_req_id

        return result

    def get_url(self, domain):
        if self.url.startswith(domain):
            return self.url[len(domain):]
        return self.url

    def pretty_type(self, type):
        if not type:
            logging.warning("Empty request type for url '%s'" % self.url)
            return "Other"
        tail = self.url.lower()[-6:]
        if tail.endswith('.js'):
            return "Script"
        if tail.endswith('.css'):
            return "Stylesheet"
        if tail.endswith('.png') or tail.endswith('.gif') or tail.endswith('.jpg') or tail.endswith('.ico'):
            return "Image"
        if type in self.types:
            return type
        if type.startswith("text/html") or tail.endswith(".html"):
            return "Document"
        return "Other"

    def set_type(self, type):
        self.type = self.pretty_type(type)

    def start(self, ts=None):
        self.ts_start = ts if ts else int(time.time() * 1000)
        self.page.browser.log_debug(" req %s started   - %d %s %s %s %s" %
                                    (self.id, self.ts_start, self.method, self.url, self.header, self.params))

    def complete(self, ts=None):
        if not ts:
            ts = int(time.time() * 1000)
        self.ts_end = ts
        self.dur = int(round(self.ts_end - self.ts_start))
        self.completed = True
        self.page.browser.log_debug(" req %s completed - %d %s %s %s - %s, %sKA, %sGzip, %sCached,"
                                    " len %d, content-len %d, dur %d ms" %
                                    (self.id, ts, self.method, self.url, self.status, self.type,
                                     "+" if self.keepalive else "-",
                                     "+" if self.gzipped else "-",
                                     "+" if self.cached else "-",
                                     self.length, self.content_length, self.dur))

    def is_long_poll(self, longpolls=None):
        if self.longpoll:
            return True

        if not longpolls:
            return False
        for l in longpolls:
            if l in self.url:
                self.longpoll = True
                return True
        return False

    def is_oK(self):
        if self.longpoll:
            return True
        try:
            base = int(self.status / 100)
        except TypeError:
            return False
        return base in (0, 2, 3)

    def validate_response(self, data):
        if self.validator is not None:
            if data.find(self.validator) < 0:
                data = data.decode('utf-8').encode('ascii', 'ignore')
                from browser import BrowserExc
                raise BrowserExc('%s %s validation failed\nvalidator: %s\ndata: %s' %
                                 (self.method, self.url, self.validator, data))
        return data

    def update_netloc(self, url):
        prot, netloc, _ = parse_url(url)
        self.url = "%s://%s%s" % (prot, netloc, self.url)


class Page:
    def __init__(self, browser, url, cached=True, longpolls=None, name=None, real_navigation=True):
        self.browser = browser
        self.browser_pid = browser.pid if browser else 0  # denormalization required for faster serialization
        self.requests = []  # append in add_request
        self.requests_groups = []  # append on complete()
        self._id2request = {}
        self.longpolls = longpolls

        # FIXME: must be moved to cp_webdriver
        # FIXME: /gelf is a telemtry page on graylog.ap.int.zone/gelf that hangs sometime
        if not self.longpolls:
            self.longpolls = ["notifications?channel=", "notifications/channel", "/gelf", "/api/subscriptions"]

        self.ram_usage_kb = 0
#        self.time_start_utc = int(time.time() * 1000)
        self.ts_start = None
        self.ts_end = None
        self.dur = 0
        self.url = url
        self.name = name
        self.cached = cached
        if url:
            self.domain = parse_url(url, server=True)

        self.length = 0
        self.timeline = PageTimeline(self)
        self.real_navigation = real_navigation  # True - we can trust browser navigation API, false - we can't

        if not self.real_navigation:
            self.cached = True  # FIXME: enforce cached=True for DOM-clicks, otherwise it looks strange in summary

        # page content
        self.data = ""

    def __str__(self):
        return self.url

    def __unicode__(self):
        return self.url

    def __deepcopy__(self, memo):
        # override deepcopy to:
        # 1. avoid 'browser' object cloning
        # 2. reset timelines

        cls = self.__class__
        result = cls(None, self.url, cached=self.cached, longpolls=self.longpolls, name=self.name)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            if k == "browser":
                result.browser = v
            elif k == "timeline":
                result.timeline = None
            else:
                setattr(result, k, copy.deepcopy(v, memo))
        result.ts_start = None
        result.ts_end = None
        result.timeline = PageTimeline(result)
        return result

    def add_request(self, req):
        if req.id in self._id2request:
            self._id2request[req.id] = req
            for n in range(0, len(self.requests)):
                if self.requests[n].id == req.id:
                    self.requests[n] = req
                    return
        else:
            self._id2request[req.id] = req
            self.requests.append(req)

    def del_request(self, req):
        if req.id in self._id2request:
            del self._id2request[req.id]
            for n in range(0, len(self.requests)):
                if self.requests[n].id == req.id:
                    del self.requests[n]
                    return

    def get_request(self, id):
        if id in self._id2request:
            return self._id2request[id]
        return None

    def process_activity(self, name, timestamp):
        if not self.ts_start or self.ts_start > timestamp:
            self.ts_start = timestamp
            self.browser.log_debug("First detected activity '%s' timestamp: %d" % (name, self.ts_start))

        if not self.ts_end or self.ts_end < timestamp:
            self.ts_end = timestamp
            self.browser.log_debug("Last detected activity '%s' timestamp: %d" % (name, self.ts_end))

    def get_incomplete_reqs(self):
        return [r for r in self.requests if not r.completed and not r.is_long_poll(self.longpolls)]

    def get_uncached_reqs(self):
        return [r for r in self.requests if not r.cached]

    def get_error_reqs(self):
        return [r for r in self.requests if not r.is_ok()]

    def get_repeated_reqs_cnt(self):
        urls = [r.url for r in self.get_uncached_reqs()]
        return len(urls) - len(set(urls))

    def get_foreign_reqs(self):
        ret = []
        if not hasattr(self, '__page_netlocs'):
            _, netloc, _ = parse_url(self.url)
            self.__page_netlocs = [netloc, netloc[4:] if netloc.startswith("www.") else "www." + netloc]

        for r in self.get_uncached_reqs():
            _, netloc, _ = parse_url(r.url)
            if netloc not in self.__page_netlocs:
                ret.append(r)
        return ret

    def print_page_requests_stats(self, title=True, description=None):
        print("")
        if title and not isinstance(title, str):
            title = "Navigation's network requests summary"
        if title:
            PageStats.print_title(title)

        PageStats.print_description(description)

        t = TextTable()
        t.add_row(["Type", "Requests", "non-200ok", "non-KA", "non-GZ", "Recv(KB)", "RecvAvg(KB)", "DurAvg(ms)"])
        t.add_row("-")

        uncached = self.get_uncached_reqs()

        for type in PageRequest.types + ["Total"]:
            if type == "Total":
                t.add_row("-")
                items = uncached
            else:
                items = [r for r in uncached if r.type == type]

            if not items:
                continue

            L = sum([r.length for r in items])
            d = sum([r.dur for r in items])
            t.add_row([PageRequest.types_abbr[type],
                       len(items),
                       len([r for r in items if r.status != 200]),
                       len([r for r in items if not r.keepalive]),
                       len([r for r in items if not r.gzipped]),
                       "%8s" % ("%.1f" % (float(L) / 1024)),
                       "%11s" % ("%.1f" % (float(L) / 1024 / len(items))),
                       "%10s" % ("%.0f" % (float(d) / len(items)))
                       ])

        print("  " + "\n  ".join(t.get_lines()))

    def print_page_req_groups_stats(self, title=True, description=None):
        print("")
        if title and not isinstance(title, str):
            title = "Navigation network activity grouped by independent requests"
        if title:
            PageStats.print_title(title)
            print("  Every *group* is separated by the ---- line, group includes independent requests only")
            print("  - Sta   - response HTTP status code")
            print("  - Recv  - length of received data")
            print("  - AX    - is Ajax request")
            print("  - KA    - keep-alive enabled")
            print("  - GZ    - response compressed by gzip\n")

        PageStats.print_description(description)

        t = TextTable(max_col_width=[0, 0, 0, 0, 0, 0, 0, 0, 0, 90], col_separator=" ", left_aligned=[0, 9])
        t.add_row(["Typ", "Sta", " Recv", "AX", "KA", "GZ", "Start", " Dur", "  End", "Url"])
        t.add_row(["", "", " (KB)", "", "", "", " (ms)", "(ms)", " (ms)", ""])

        for g in self.requests_groups:
            reqs = g.get_uncached_reqs()
            if not reqs:
                continue

            t.add_row("-")
            for r in sorted(reqs, key=lambda x: x.ts_start):
                t.add_row(["%s" % PageRequest.types_abbr[r.type],
                           r.status,
                           "%5s" % ("%.1f" % (r.length / 1024.0)),
                           "ax" if (r.ts_start - self.ts_start) > self.timeline.ajax_start else " -",
                           "ka" if r.keepalive else " -",
                           "gz" if r.gzipped else " -",
                           int(round(r.ts_start - self.ts_start)),
                           r.dur,
                           int(round(r.ts_start + r.dur - self.ts_start)),
                           r.get_url(self.domain)])
        print("  " + "\n  ".join(t.get_lines()))

    def start(self, ts=None):
        self.ts_start = ts if ts else int(time.time() * 1000)

    def complete(self, browser, ts=None):
        if not self.ts_end:
            ends = [req.ts_end for req in self.requests]
            self.ts_end = max(ends) if ends else self.timeline.values['ajaxEnd']

        self.ram_usage_kb = browser.browser_get_ram_usage_kb()

        if self.real_navigation:
            self.timeline = browser.browser_get_page_timeline(self)
            if self.timeline.values['navStrt']:
                # Ok, it was real navigation request and we can trust browser navigation API
                if len(self.requests):
                    if not self.ts_start:
                        self.browser.log_error("BUG: page %s load start time is empty" % str(self.url))
                    offt = self.timeline.values['navStrt'] - self.ts_start
                else:
                    offt = 0
                    self.ts_start = self.timeline.values['navStrt']

                self.browser.log_debug("Adjusting page/requests timestamp by %d ms" % round(offt))

                self.ts_start += offt
                self.ts_end += offt
                for r in self.requests:
                    r.ts_start += offt
                    if r.is_long_poll(self.longpolls):
                        self.browser.log_debug("Skip '%s' longpoll request" % r.url)
                        continue
                    elif not r.ts_end:
                        self.browser.log_error("Can't determine '%s' request completion ts, ignored!" % r.url)
                        r.status = "timeout"
                    else:
                        r.ts_end += offt
                for g in self.requests_groups:
                    g.ts_start += offt
                    g.ts_end += offt

        self.length = 0
        for r in self.requests:
            if r.content_length:
                r.length = r.content_length
            if not r.cached:
                self.length += r.length

        self.dur = max(self.ts_end - self.ts_start, self.timeline.total_dur)

        for r in sorted([r for r in self.requests if not r.is_long_poll(self.longpolls)], key=lambda x: x.ts_start):
            if not len(self.requests_groups) or not self.requests_groups[-1].add_request(r):
                self.requests_groups.append(PageRequestsGroup(r))

        # self.data = "".join([req.data for req in self.requests if req.url == self.url])
        for req in self.requests:
            if req.url == self.url:
                if type(req.data) == bytes:
                    self.data += req.data.decode('utf-8')
                else:
                    self.data += req.data

    def get_full_name(self, url_prefix_to_remove=""):
        if self.name:
            return self.name
        return self.url[len(url_prefix_to_remove):]

    def get_key(self, name_priority=False):
        return (self.name if self.name else self.url.split("?bw_id")[0], self.cached)

    def serialize(self, use_pickle=True):
        self.browser.log_debug("serializing page: %s, %d, %s" % (str(int(self.ts_start)), int(self.dur), self.url))
        if use_pickle:
            p = copy.deepcopy(self)
            p.ts_start = self.ts_start  # it was zeroed by deepcopy()
            p.ts_end = self.ts_end  # it was zeroed by deepcopy()
            p.browser = None  # detach real browser object since it has open files
            return base64.urlsafe_b64encode(pickle.dumps(p)) + "\n"

        return "%s|%1.3f|%6d|%2d|%s|%s|%s|%d|%s\n" % \
               (str(int(self.ts_start)), self.dur / 1000.0, self.length, len(self.get_uncached_reqs()),
                "C" if self.cached else "U", self.name if self.name else "", self.browser_pid, str(self.id), self.url)

    @staticmethod
    def deserialize(line, browser_class, use_pickle=True):

        if use_pickle:
            p = pickle.loads(base64.urlsafe_b64decode(line.strip()))
            p.ts_start = int(p.ts_start)
            p.ts_end = int(p.ts_end)
            return p

        ar = line.split("|")
        if len(ar) < 7:
                    raise Exception("can't parse line: %s" % line)

        url = ar[7].strip()

        p = Page(None, url)
        p.ts_start = int(ar[0])
        p.dur = float(ar[1].strip()) * 1000.0
        p.ts_end = int(p.ts_start) + int(round(p.dur, 0))
        p.length = int(ar[2])
        p.requests = [] * int(ar[3])
        p.cached = ar[4] == "C"
        p.browser_pid = ar[5].strip()
        p.name = ar[6].strip() if ar[5] else None
        p.url = url

        return p


class PageStats:
    separator = "-->"
    width = 118

    def __init__(self, id=""):
        self.iterations = []
        self.id = id

    @staticmethod
    def print_title(title):
        print(title.upper())
        print("=" * len(title))
        print("")

    @staticmethod
    def print_description(description):
        if description:
            print("  " + "\n  ".join(description))
            print("")

    def print_page_timeline_header(self, title=True, description=None):
        print("")
        if title and not isinstance(title, str):
            title = "Page(s) timeline and memory usage"
        if title:
            self.print_title(title)

            j = PageTimeline.jstypes
            print("  http://www.w3.org/TR/navigation-timing/timing-overview.png")
            print("  http://www.w3.org/TR/navigation-timing/#sec-navigation-timing-interface")
            print("  - navStrt    - %s - navigation start" % j['navStrt'])
            print("  - reqStrt    - %s - browser has resolved the domain (and redirects)"
                  " and started the request" % j['reqStrt'])
            print("  - rspStrt    - %s - server started response (TTFB)" % j['rspStrt'])
            print("  - rspEnd     - %s - browser received last byte of the document" % j['rspEnd'])
            print("  - domEnd     - %s - browser has downloaded all the CSS, JS and rendered the DOM model" %
                  j['domEnd'])
            print("  - onloadEnd  - %s - browser completed the onload() callbacks" % j['onloadEnd'])
            print("  - ajaxEnd    - last detected browser activity, all pending img/css/js and Ajax requests"
                  " have been completed")
            print("  - Total(ms)  - total time of page (TTLB) including all the img/css/js and Ajax requests")
            print("  - MemUsg(KB) - page memory usage (RSS delta between browser start and after page fully loaded)")
            print("")

        self.print_description(description)

        print("  Iter # " + " |", end=" ")
        for n in range(0, len(PageTimeline.types) - 1):
            print("%s %s" % (PageTimeline.types[n], self.separator), end=" ")
        print(PageTimeline.types[-1], end=" ")
        print("| Total(ms) | MemUsg(KB)")
        print("  " + "-" * self.width)

    @staticmethod
    def print_summary(pages_stats, title="Summary", perf_atomic_format=False):
        print("")
        if title and not isinstance(title, str):
            title = "Summary"
        if title:
            PageStats.print_title(title)

        t = TextTable(left_aligned=[0], max_col_width=[72])
        t.add_row(["Screen", "Iters", "   Requests per page   ", "RecvAvg", "Total", "MemUsg"])
        t.add_row(["", "", "Ntwrk  Rptd  Frgn  Errs", "   (KB)", " (ms)", "  (KB)"])
        t.add_row("-")

        urls = set()
        for ps in pages_stats:
            for p in ps.iterations:
                for r in p.requests:
                    urls.add(r.url)

        pfx = get_common_url_prefix(urls)

        def browser_dict():
            return defaultdict(url_dict)

        def url_dict():
            return defaultdict(int)

        def status_dict():
            return defaultdict(int)

        reqs = defaultdict(browser_dict)

        perf_atomic_output = []

        prev_psid = ""
        for ps in pages_stats:
            n = 1.0 * len(ps.iterations)
            size = sum([p.length for p in ps.iterations]) / (1024 * n)
            errs = sum([len(p.get_error_reqs()) for p in ps.iterations]) / n
            repeated = sum([p.get_repeated_reqs_cnt() for p in ps.iterations]) / n
            foreign = sum([len(p.get_foreign_reqs()) for p in ps.iterations]) / n
            dur = sum([p.dur for p in ps.iterations]) / n
            if prev_psid != ps.id:
                t.add_row(str(ps.id) + ":")
                prev_psid = ps.id

            screen = ps.iterations[0].get_full_name(pfx)
            t.add_row(["  " + screen, int(n),
                       ("%5.0f  %4s  %4s  %4s") %
                       (sum([len(p.get_uncached_reqs()) for p in ps.iterations]) / n,
                        "%4.0f" % repeated if repeated else "-",
                        "%4.0f" % foreign if foreign else "-",
                        "%.1f !" % (errs) if errs else "-"),
                       "%.1f" % size,
                       "%.0f" % dur,
                       "%.0f" % (sum([p.ram_usage_kb for p in ps.iterations]) / n)])

            if perf_atomic_format:
                perf_atomic_output.append("test: %s: %s; loops: %d; time: %.3f; rate: { %.3f } sec; less_better: 1;" %
                                          (ps.id, screen, int(n), int(dur * int(n)) / 1000.0, int(dur) / 1000.0))

            for p in ps.iterations:
                for r in p.requests:
                    reqs[ps.id][r.url]['all'] += 1
                    if not r.is_ok():
                        reqs[ps.id][r.url][r.status] += 1

        errs = {}
        for b, items in reqs.items():
            for url, statuses in items.items():
                for status, count in reqs[b][url].items():
                    if status == 'all':
                        continue
                    total = reqs[b][url]['all']
                    if b not in errs:
                        errs[b] = []
                    errs[b].append(["  " + url[len(pfx):], status, count, "%.1f" % (100 * count / (1.0 * total))])

        print("  " + "\n  ".join(t.get_lines()))

        if len(errs):
            print("")
            PageStats.print_title("Warning: error network requests detected !!!")

            wt = TextTable(left_aligned=[0, 1], max_col_width=[80])
            wt.add_row(["URL", "Status", "Count", "% of total"])
            wt.add_row("-")
            for b, rows in errs.items():
                wt.add_row(b + ":")
                for row in rows:
                    wt.add_row(row)
            print("  " + "\n  ".join(wt.get_lines()))

        if perf_atomic_output:
            print("")
            PageStats.print_title("perf-atomic output format")
            print("\n".join(perf_atomic_output))

    def add_iteration(self, page):
        self.iterations.append(page)

    def print_page_timeline(self, p, title="", hr=False):
        if not p:
            return

        if hr:
            print("  " + "-" * self.width)

        print("  %7s |" % title, end=" ")
        t = p.timeline
        for n in range(0, len(t.deltas)):
            print("%s%5d" % (" " * (len(PageTimeline.types[n]) - 4 + len(self.separator)), t.deltas[n]), end=" ")
        print("%s" % (" " * len(PageTimeline.types[-1])), end=" ")
        print("| %9d | %10d" % (p.dur, p.ram_usage_kb))

    def get_avg(self, iterations=None):
        if not iterations:
            iterations = self.iterations
        if not iterations or len(iterations) < 2:
            return None

        avg = Page(None, "", None)
        avg.iterations = len(iterations)
        for p in iterations:
            t = p.timeline
            for d in range(0, len(t.deltas)):
                avg.timeline.deltas[d] += t.deltas[d]
            avg.dur += p.dur

        for d in range(0, len(t.deltas)):
            avg.timeline.deltas[d] = int(avg.timeline.deltas[d] / len(iterations))
        avg.dur = int(avg.dur / len(iterations))
        avg.ram_usage_kb = sum([p.ram_usage_kb for p in iterations]) / len(iterations)
        return avg


# Represents some kind of the html page model with focus on actions (URLs)
class PageWithActions:
    def __init__(self, actions, body, url):
        self.actions = actions  # actions (urls) found on page
        self.body = body  # page body (html text)
        self.url = url  # page source url
