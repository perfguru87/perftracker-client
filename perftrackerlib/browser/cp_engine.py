#!/usr/bin/env python

from __future__ import print_function, absolute_import

# -*- coding: utf-8 -*-
__author__ = "perfguru87@gmail.com"
__copyright__ = "Copyright 2018, The PerfTracker project"
__license__ = "MIT"

"""
Any control panel engine (helper)
"""

import sys
import os
import logging
import time
import re

from .browser_base import BrowserBase, BrowserExc, BrowserExcTimeout, DEFAULT_WAIT_TIMEOUT
from .browser_python import BrowserPython
from selenium.common.exceptions import ElementNotVisibleException
from selenium.common.exceptions import NoSuchElementException
from selenium.common.exceptions import WebDriverException
from selenium.common.exceptions import StaleElementReferenceException


reHTML = re.compile('<.*?>')


def remove_html_tags(text):
    return re.sub(reHTML, '', text)


class CPMenuItemXpath:
    def __init__(self, level, frame, link_xpath, title_xpath, menu_url_clicks=True, menu_dom_clicks=True):
        self.level = level  # menu level, 0 - 10...
        self.frame = frame  # menu frame name
        self.link_xpath = link_xpath  # clickable menu item element xpath 
        self.title_xpath = title_xpath  # relative xpath to fetch the menu item title
        self.menu_url_clicks = menu_url_clicks
        self.menu_dom_clicks = menu_dom_clicks


class CPMenuItem:
    def __init__(self, level, title, link, xpath, parent, menu_url_clicks=True, menu_dom_clicks=True):
        """
        menu_url_clicks - collect direct URL links to menu items
        menu_dom_clicks - collect DOM (xpath) links to menu items
        """
        self.level = level
        self.link = link
        self.xpath = xpath
        self.children = []
        self._scanned_menu_items = set()

        self.parent = parent
        if parent:
            self.title = parent.title + " -> " + title
        else:
            self.title = title
        self.menu_url_clicks = menu_url_clicks
        self.menu_dom_clicks = menu_dom_clicks

        if link:
            print("  %s - %s" % (self.title, link))  # ugly :-(

    def is_scanned(self, key):
        if self.parent:
            return self.parent.is_scanned(key)
        return key in self._scanned_menu_items

    def mark_as_scanned(self, key):
        p = self
        while p.parent:
            p = p.parent
        p._scanned_menu_items.add(key)

    def add_child(self, title, link, xpath, menu_xpath):
        ch = CPMenuItem(self.level + 1, title, link, xpath, self,
                        menu_url_clicks=self.menu_url_clicks and menu_xpath.menu_url_clicks,
                        menu_dom_clicks=self.menu_dom_clicks and menu_xpath.menu_dom_clicks,
                        )
        self.children.append(ch)

        self.mark_as_scanned(title)
        self.mark_as_scanned(link)
        return ch

    def get_items(self, items=None):
        """ return a list of [item#, title, link] """
        if not items:
            items = {}
        for c in self.children:
            if self.menu_url_clicks:
                items[c.link] = [len(items), c.title, c.link]
            if self.menu_dom_clicks and c.xpath:
                xpath = "%s^%s" % (c.link, c.xpath)
                items[xpath] = [len(items), c.title + " (DOM click)", xpath]
            items = c.get_items(items)
        return items


class CPEngineBase:
    type = "A control panel"
    menu_url_clicks = True  # collect direct URL links to menu items
    menu_dom_clicks = True  # collect DOM (xpath) links to menu items
    menu_xpaths = []  # [[CPMenuItemXpath(0, ...), ...], [CPMenuItemXpath(1, ...), ...]]

    def __init__(self, browser):
        self.browser = browser
        self.log_error = browser.log_error
        self.log_warning = browser.log_warning
        self.log_info = browser.log_info
        self.log_debug = browser.log_debug
        self.menu = CPMenuItem(0, self.type, None, None, None,
                               menu_url_clicks=self.menu_url_clicks, menu_dom_clicks=self.menu_dom_clicks)
        self.current_frame = None

    def init_context(self):
        return True

    def get_current_url(self, url=None):
        if url and url.lower().find('javascript') < 0:
            return url
        return self.browser.browser_get_current_url()

    def get_menu_item_title(self, title_el):
        return remove_html_tags(title_el.get_attribute("innerHTML"))

    def get_current_xpath(self, link_el):
        if not link_el:
            return None

        xpath = "a"

        parent = link_el
        tag = "a"

        while True:
            parent = parent.find_element_by_xpath("..")
            if not parent:
                break

            id = parent.get_attribute('id')
            tag = parent.tag_name

            if id:
                xpath = "%s[@id='%s']/%s" % (tag, id, xpath)
                break

            xpath = "%s/%s" % (tag, xpath)

        xpath = "//%s" % xpath
        return xpath

    def skip_menu_item(self, link_el, title):
        return False

    def switch_to_frame(self, frame, verbose=True):
        if not frame:
            return None
        self.switch_to_default_content()

        self.browser.log_info("searching for frame: '%s'" % frame)
        try:
            el = self.browser.driver.find_element_by_xpath(frame)
        except NoSuchElementException:
            if verbose:
                self.browser.log_error("Can't find frame element: '%s', page source:\n%s" %
                                       (frame, self.browser.driver.page_source))
            return None
        self.browser.dom_switch_to_frame(el)
        self.current_frame = frame
        return el

    def switch_to_default_content(self):
        self.current_frame = None
        self.browser.dom_switch_to_default_content()

    def dom_click(self, el, title=None):
        self.browser.dom_click(el, name=title)

    def menu_item_click(self, el, timeout_s=DEFAULT_WAIT_TIMEOUT, title=None):
        def wait_callback(self, el, timeout_s, title):
            self.browser.dom_wait_element_stale(el, timeout_s=timeout_s, name=title)

        self.browser.dom_click(el, timeout_s=timeout_s, name=title,
                               wait_callback=wait_callback, wait_callback_obj=self)

    def _populate_menu(self, menu):

        if len(self.menu_xpaths) <= menu.level:
            return

        for x in self.menu_xpaths[menu.level]:

            if x.frame:
                frame = self.switch_to_frame(x.frame, verbose=False)
                if not frame:
                    continue

            self.log_debug("Looking for xpath: '%s'" % x.link_xpath)
            menu_elements = self.browser.driver.find_elements_by_xpath(x.link_xpath)
            i = 0
            while i < len(menu_elements):
                link_el = menu_elements[i]
                try:
                    link = link_el.get_attribute('href')
                except StaleElementReferenceException:
                    # previous click caused dom change, so re-load menu items (assuming their sequence is preserved)
                    menu_elements = self.browser.driver.find_elements_by_xpath(x.link_xpath)
                    if i >= len(menu_elements):
                        continue  # menu has shrunk suddenly
                    link_el = menu_elements[i]
                    link = link_el.get_attribute('href')
                i += 1

                if not link:
                    link = link_el.get_attribute('innerHTML')

                self.log_info("found menu item element: '%s'" % link)

                if x.title_xpath:
                    title_els = link_el.find_elements_by_xpath(x.title_xpath)
                    if not title_els or not len(title_els) or not title_els[0].get_attribute("innerHTML"):
                        self.log_error("WARNING: can't get title for menu element: %s\nusing: %s" % (link, x.title_xpath))
                        continue
                    title_el = title_els[0]
                else:
                    title_el = link_el

                title = self.get_menu_item_title(title_el)
                if menu.is_scanned(title):
                    self.log_debug("skipping menu item '%s', it was already scanned" % title)
                    continue

                if link == "javascript:void(0);":
                    self.log_debug("skipping void link in '%s'" % title)
                    continue

                if self.skip_menu_item(link_el, title):
                    self.log_debug("skipping menu item '%s'" % title)
                    continue

                curr_xpath = self.get_current_xpath(link_el)

                self.log_info("clicking on the '%s' menu item, link %s" % (title, link))
                try:
                    self.menu_item_click(link_el, title=title)
                except WebDriverException as e:
                    self.log_info(" ... skipping the '%s' menu item: %s" % (title, str(e)))
                except ElementNotVisibleException:
                    self.log_info(" ... skipping the '%s' menu item since it is not visible" % title)
                    continue
                except BrowserExc as e:
                    self.log_debug("dom_click() raised exception: %s" % str(e))
                    self.log_warning("WARNING: can't wait for '%s' " % (title) + "menu click completion, skipping it")
                    continue

                curr_url = self.get_current_url(link)
                self.browser.history.append(curr_url)

                if menu.is_scanned(curr_url):
                    self.log_debug("skipping menu item with url '%s', it was already scanned" % curr_url)
                    continue

                self.log_info(" ... '%s' menu item has URL: %s, xpath: %s" % (title, curr_url, curr_xpath))

                ch = menu.add_child(title, curr_url, curr_xpath, x)
                self._populate_menu(ch)
                self._populate_menu(menu)
                break

            if x.frame:
                self.switch_to_default_content()

    def do_menu_walk(self):
        self.browser.print_stats_title("Control panel menu scanner...")
        print("Control panel detected: '%s'" % self.type)  # ugly :-(
        print("Searching for menu items...\n")  # ugly :-(
        self.menu.link = self.get_current_url()
        self._populate_menu(self.menu)
        self.log_info("Menu scan completed")
        items = self.menu.get_items()
        return [(i[1], i[2]) for i in sorted(items.values(), key=lambda x: x[0])]


##############################################################################
# Autotests
##############################################################################


if __name__ == "__main__":
    CPEngineBase(BrowserPython())
    print("OK")
