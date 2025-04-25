from datetime import datetime
import re
import time
from queue import Queue, Empty
from urllib.parse import parse_qs, urlparse, quote

from calibre.ebooks.metadata.book.base import Metadata
from calibre.ebooks.metadata.sources.base import Source
from calibre.ebooks.chardet import xml_to_unicode
from calibre.utils.cleantext import clean_ascii_chars
from lxml import etree
from lxml.html import tostring

# note that string passed-in will need to be url-encoded
# jinjiang may use site search or bing cn search in a iframe
# t=1 is book title, t=2 is author
# ord=novelsize is sort by book size
JINJIANG_SEARCH_URL = "https://www.jjwxc.net/search.php?kw=%s&ord=novelsize&t=1"
JINJIANG_BOOK_URL = "https://www.jjwxc.net/onebook.php?novelid=%s"
JINJIANG_BOOK_URL_PATTERN = re.compile(".jjwxc\\.net\\/onebook\\.php\\?novelid=(\\d+)")
# there's a coverid param which I'm not sure what it's used for
JINJIANG_BOOKCOVER_URL = "https://i9-static.jjwxc.net/novelimage.php?novelid=%s"

PROVIDER_ID = "jinjiang"
PROVIDER_VERSION = (1, 2, 6)
PROVIDER_AUTHOR = "Otaro"


class SearchResultIndexMetadataCompareKeyGen:
    def __init__(self, mi):
        self.extra = getattr(mi, 'search_result_index', 0)

    def compare_to_other(self, other):
        return self.extra - other.extra

    def __eq__(self, other):
        return self.compare_to_other(other) == 0

    def __ne__(self, other):
        return self.compare_to_other(other) != 0

    def __lt__(self, other):
        return self.compare_to_other(other) < 0

    def __le__(self, other):
        return self.compare_to_other(other) <= 0

    def __gt__(self, other):
        return self.compare_to_other(other) > 0

    def __ge__(self, other):
        return self.compare_to_other(other) >= 0


def parse_html(raw):
    try:
        from html5_parser import parse
    except ImportError:
        # Old versions of calibre
        import html5lib

        return html5lib.parse(raw, treebuilder="lxml", namespaceHTMLElements=False)
    else:
        return parse(raw)


# a metadata download plugin
class Jinjiang(Source):
    name = "jjwxc.net"  # Name of the plugin
    description = "Downloads metadata and covers from Jinjiang."
    supported_platforms = [
        "windows",
        "osx",
        "linux",
    ]  # Platforms this plugin will run on
    author = PROVIDER_AUTHOR  # The author of this plugin
    version = PROVIDER_VERSION  # The version number of this plugin
    minimum_calibre_version = (5, 0, 0)
    capabilities = frozenset(["identify", "cover"])
    touched_fields = frozenset(
        [
            "title",
            "authors",
            "identifier:" + PROVIDER_ID,
            "comments",
            "publisher",
            "languages",
            "tags",
            "pubdate",
        ]
    )
    has_html_comments = True
    supports_gzip_transfer_encoding = True
    can_get_multiple_covers = True

    def __init__(self, *args, **kwargs):
        Source.__init__(self, *args, **kwargs)

    def get_book_url(self, identifiers):
        jj_id = identifiers.get(PROVIDER_ID, None)
        if jj_id:
            return (PROVIDER_ID, jj_id, JINJIANG_BOOK_URL % jj_id)
        return None

    def get_book_url_name(self, idtype, idval, url):
        return "晋江文学城"

    def get_cached_cover_url(self, identifiers):
        jj_id = identifiers.get(PROVIDER_ID, None)
        if jj_id:
            return JINJIANG_BOOKCOVER_URL % jj_id
        return None

    def id_from_url(self, url):
        res = JINJIANG_BOOK_URL_PATTERN.findall(url)
        if len(res) == 1:
            return res[0]
        return None
    
    def identify_results_keygen(self, title=None, authors=None, identifiers={}):
        def keygen(mi):
            return SearchResultIndexMetadataCompareKeyGen(mi)
        return keygen

    def identify(
        self,
        log,
        result_queue,
        abort,
        title=None,
        authors=None,
        identifiers={},
        timeout=30,
    ):
        jj_id = identifiers.get(PROVIDER_ID, None)
        if jj_id:
            url = JINJIANG_BOOK_URL % jj_id
            log.info("identify with jinjiang id (%s) from url: %s" % (jj_id, url))
            br = self.browser
            try:
                raw = br.open_novisit(url, timeout=timeout).read().strip()
            except Exception as e:
                log.exception(e)
                return None

            raw = clean_ascii_chars(
                xml_to_unicode(raw, strip_encoding_pats=True, resolve_entities=True)[0]
            )

            try:
                root = parse_html(raw)
            except Exception as e:
                log.exception(e)
                return None

            title = root.xpath('//span[@itemprop="articleSection"]')[0].text
            author = root.xpath('//span[@itemprop="author"]')[0].text
            desc = tostring(
                root.xpath('//div[@id="novelintro"]')[0],
                method="html",
                encoding="utf-8",
            ).strip()
            tags = list(
                map(
                    lambda elem: elem.text,
                    root.xpath('//div[@class="smallreadbody"]/span[contains(text(), "标签")]/following-sibling::span/a'),
                )
            )

            custom_cover: str = root.xpath('//img[@class="noveldefaultimage"]')[0].get("src", '')
            if custom_cover:
                cover_url = custom_cover
                parsed = urlparse(custom_cover)
                if "authorspace" in parsed.netloc:
                    # 'authorspace' meaning it's uploaded to jinjiang author space.
                    # Remove the tailing '_300_420' to get original image
                    cleaned_path = re.sub(r"_300_420(?=\.\w+$)", "", parsed.path)
                    cover_url = f"{parsed.scheme}://{parsed.netloc}{cleaned_path}"
            else:
                cover_url = JINJIANG_BOOKCOVER_URL % jj_id

            log.info("custom cover url: %s" % cover_url)

            first_chapter = root.xpath('//table//tr[contains(@itemprop, "chapter")]')[0]
            if first_chapter is not None:
                # chapterclick is available on non-VIP chapters only
                chapter_id = first_chapter.xpath('td[@class="chapterclick"]')[0].get("clickchapterid", None)

                if chapter_id == "1":
                    combined_time_string = first_chapter.xpath('td[last()]')[0].get("title", None)
                    marker = "章节首发时间："
                    pattern = rf"{re.escape(marker)}(\d{{4}}-\d{{2}}-\d{{2}}\s\d{{2}}:\d{{2}}:\d{{2}})"

                    match = re.search(pattern, combined_time_string)
                    if match:
                        datetime_str = match.group(1)
                        log.info("found publish date: %s" % datetime_str)
                        bPublishDate = datetime.strptime(
                            datetime_str, "%Y-%m-%d %H:%M:%S"
                        )
                else:
                    log.error("first chapter's chapter_id is not 1, something wrong with the book")

            mi = Metadata(title, [author])
            mi.identifiers = {PROVIDER_ID: jj_id}
            mi.comments = desc
            mi.publisher = "晋江文学城"
            mi.language = "zh_CN"
            mi.tags = tags
            mi.url = JINJIANG_BOOK_URL % jj_id
            mi.cover = cover_url
            mi.pubdate = bPublishDate

            result_queue.put(mi)
            return

        # jinjiang will try to use its own search engine first
        # if that fails, it will use bing cn / baidu (bing cn is default)
        # bing cn is smart enough to search with title+author, even with misspelling
        normalized_authors = [] if authors is None else authors
        normalized_title = "" if title is None else title
        search_url = JINJIANG_SEARCH_URL % quote(normalized_title, encoding="gb18030")

        log.info("identify with title (%s) from url: %s" % (normalized_title, search_url))

        br = self.browser
        try:
            raw = br.open_novisit(search_url, timeout=timeout).read().strip()
        except Exception as e:
            log.exception(e)
            return

        raw = clean_ascii_chars(
            xml_to_unicode(raw, strip_encoding_pats=True, resolve_entities=True)[0]
        )

        try:
            root = parse_html(raw)
        except Exception as e:
            log.exception(e)
            return

        detected_books = root.xpath('//table[@class="searchContainer"]//div[@class="nav"]')[0]
        if detected_books is not None:
            div_text_cleaned = detected_books.xpath('string(.)').strip()
            log.info("detected books: %s" % div_text_cleaned)

            # Use regex to extract the number
            match = re.search(r"共找到 (\d+) 篇文章", div_text_cleaned)
            if match:
                number = int(match.group(1))
                log.info("found %d books from jinjiang search engine " % number)

                if number == 0:
                    # this is bing cn search
                    books = root.xpath('//ol[@id="b_results"]/li[@class="b_algo"]')
                    log.info("found %d books from bing search" % len(books))

                    # @TODO: supports bing search result
                    # @TODO: redo search with title+author if authors is not None
                else:
                    # @TODO: supports paging
                    books = root.xpath('//div[@id="search_result"]/div[not(@style) and not(@class)]')
                    log.info("found %d books on first page" % len(books))

                    for i, book in enumerate(books):
                        # we'll use the search page result for the sake of speed
                        # with the trade-off of no tags, default cover pic, HMS of publish date and un-formatted desc
                        # @TODO: go into detail page to get the tags, cover pic, publish date and desc
                        bURL: str = book.xpath('h3[@class="title"]/a')[0].get("href", "")
                        if bURL:
                            novel_id = self.id_from_url(bURL)
                            if novel_id is None:
                                log.error("[%d] can't find book id from url: %s" % (i, bURL))
                                continue
                        else:
                            log.error("[i] can't find book url from search result" % (i))
                            continue
                        bTitle = book.xpath('h3[@class="title"]//span')[0].text
                        bPublishDate = datetime.strptime(
                            book.xpath('h3[@class="title"]/font')[0]
                            .text.strip()
                            .replace("(", "")
                            .replace(")", ""),
                            "%Y-%m-%d",
                        )
                        bAuthor = book.xpath('div[@class="info"]/a/span')[0].text.strip()
                        bDesc = tostring(
                            book.xpath('div[@class="intro"]')[0],
                            method="html",
                            encoding="utf-8",
                        ).strip()

                        mi = Metadata(bTitle, [bAuthor])
                        mi.identifiers = {PROVIDER_ID: novel_id}
                        mi.comments = bDesc
                        mi.publisher = "晋江文学城"
                        mi.language = "zh_CN"
                        mi.tags = []
                        mi.url = bURL
                        mi.cover = JINJIANG_BOOKCOVER_URL % novel_id
                        mi.pubdate = bPublishDate
                        mi.search_result_index = i

                        log.info(
                            "[%d] id (%s) title (%s) author (%s) publish_data (%s)"
                            % (i, novel_id, bTitle, bAuthor, bPublishDate)
                        )

                        result_queue.put(mi)
            else:
                log.error("can't parsing search result: %s" % (div_text_cleaned))
        else:
            log.error(
                "can't get detected books element from search result: %s" % (search_url)
            )

    def download_cover(
        self,
        log,
        result_queue,
        abort,
        title=None,
        authors=None,
        identifiers={},
        timeout=30,
        get_best_cover=False,
    ):

        jj_id = identifiers.get(PROVIDER_ID, None)
        if jj_id is None:
            log.info("No id found, running identify")
            rq = Queue()
            self.identify(
                log, rq, abort, title=title, authors=authors, identifiers=identifiers
            )
            if abort.is_set():
                return

            results = []
            while True:
                try:
                    results.append(rq.get_nowait())
                except Empty:
                    break

            if len(results) == 0:
                log.info("no result after running identify")
                return

            results.sort(
                key=self.identify_results_keygen(
                    title=title, authors=authors, identifiers=identifiers
                )
            )

            # get the first result
            jj_id = results[0].identifiers.get(PROVIDER_ID, None)

        if jj_id is None:
            log.info("No id found after running identify")
            return
        
        br = self.browser

        # try get default cover first
        log("Downloading default cover from:", cover_url)
        try:
            time.sleep(1)
            cdata = br.open_novisit(cover_url, timeout=timeout).read()
            if cdata:
                result_queue.put((self, cdata))
        except:
            log.exception("Failed to download default cover from:", cover_url)

        # try get custom cover
        try:
            raw = br.open_novisit(JINJIANG_BOOK_URL % jj_id, timeout=timeout).read().strip()
        except Exception as e:
            log.exception(e)
            return

        raw = clean_ascii_chars(
            xml_to_unicode(raw, strip_encoding_pats=True, resolve_entities=True)[0]
        )

        try:
            root = parse_html(raw)
        except Exception as e:
            log.exception(e)
            return

        custom_cover_src: str = root.xpath('//img[@class="noveldefaultimage"]')[0].get("src", "")
        if custom_cover_src is not None:
            parsed = urlparse(custom_cover_src)
            if "authorspace" in parsed.path:
                # 'authorspace' meaning it's uploaded to jinjiang author space.
                # Remove the tailing '_300_420' to get original image
                cleaned_path = re.sub(r"_300_420(?=\.\w+$)", "", parsed.path)
                cover_url = f"{parsed.scheme}://{parsed.netloc}{cleaned_path}"
                try:
                    time.sleep(1)
                    log("Downloading authorspace 'original' custom cover from:", cover_url)
                    cdata = br.open_novisit(cover_url, timeout=timeout).read()
                    if cdata:
                        result_queue.put((self, cdata))
                except:
                    log.exception("Failed to download 'original' authorspace custom cover from:", cover_url)
                    try:
                        time.sleep(1)
                        log("Downloading authorspace low-res custom cover from:", cover_url)
                        cdata = br.open_novisit(custom_cover_src, timeout=timeout).read()
                        if cdata:
                            result_queue.put((self, cdata))
                    except:
                        log.exception("Failed to download low-res authorspace custom cover from:", cover_url)
            else:
                try:
                    time.sleep(1)
                    log("Downloading custom cover from:", cover_url)
                    cdata = br.open_novisit(custom_cover_src, timeout=timeout).read()
                    if cdata:
                        result_queue.put((self, cdata))
                except:
                    log.exception("Failed to download custom cover from:", cover_url)


if __name__ == "__main__":
    # To run these test use: calibre-debug -e ./__init__.py
    from calibre.ebooks.metadata.sources.test import (
        test_identify_plugin,
        title_test,
        authors_test,
    )

    # TODO: add test cases for cover download
    test_identify_plugin(
        Jinjiang.name,
        [
            (
                # TODO: custom cover test
                {
                    "identifiers": {"jinjiang": "3146241"},
                },
                [title_test("我五行缺你", exact=True), authors_test(["西子绪"])],
            ),
            (
                {
                    "identifiers": {"jinjiang": "2374843"},
                },
                [title_test("岁月间", exact=True), authors_test(["静水边"])],
            ),
            (
                {
                    "identifiers": {"jinjiang": "6414844"},
                },
                [title_test("信息素独占", exact=True), authors_test(["故筝"])],
            ),
            (
                {
                    "title": "天官赐福"
                },
                [title_test("天官赐福", exact=True), authors_test(["墨香铜臭"])],
            ),
        ],
    )
