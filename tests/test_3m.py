from nose.tools import (
    assert_raises_regexp,
    set_trace, 
    eq_,
)
import datetime
import os
from model import (
    Contributor,
    DataSource,
    Resource,
    Hyperlink,
    Identifier,
    Edition,
    Subject,
    Measurement,
    Work,
)
from threem import (
    ItemListParser,
    MockThreeMAPI,
    ThreeMBibliographicCoverageProvider,
)
from . import DatabaseTest
from util.http import BadResponseException

class BaseThreeMTest(object):

    base_path = os.path.split(__file__)[0]
    resource_path = os.path.join(base_path, "files", "3m")

    @classmethod
    def get_data(cls, filename):
        path = os.path.join(cls.resource_path, filename)
        return open(path).read()


class TestThreeMAPI(DatabaseTest, BaseThreeMTest):

    def setup(self):
        super(TestThreeMAPI, self).setup()
        self.api = MockThreeMAPI(self._db)

    def test_full_path(self):
        id = self.api.library_id
        eq_("/cirrus/library/%s/foo" % id, self.api.full_path("foo"))
        eq_("/cirrus/library/%s/foo" % id, self.api.full_path("/foo"))
        eq_("/cirrus/library/%s/foo" % id, 
            self.api.full_path("/cirrus/library/%s/foo" % id)
        )

    def test_full_url(self):
        id = self.api.library_id
        eq_("http://3m.test/cirrus/library/%s/foo" % id,
            self.api.full_url("foo"))
        eq_("http://3m.test/cirrus/library/%s/foo" % id, 
            self.api.full_url("/foo"))

    def test_request_signing(self):
        """Confirm a known correct result for the 3M request signing
        algorithm.
        """
        self.api.queue_response(200)
        response = self.api.request("some_url")
        [request] = self.api.requests
        headers = request[-1]['headers']
        eq_('Fri, 01 Jan 2016 00:00:00 GMT', headers['3mcl-Datetime'])
        eq_('2.0', headers['3mcl-Version'])
        expect = '3MCLAUTH b:ppuKJ2nf8OO3vCYhH3mJE8c7mjB6mGxzcPO3KOz4FTE='
        eq_(expect, headers['3mcl-Authorization'])
        
        # Tweak one of the variables that go into the signature, and
        # the signature changes.
        self.api.library_id = self.api.library_id + "1"
        self.api.queue_response(200)
        response = self.api.request("some_url")
        request = self.api.requests[-1]
        headers = request[-1]['headers']
        assert headers['3mcl-Authorization'] != expect

    def test_bibliographic_lookup(self):
        data = self.get_data("item_metadata_single.xml")
        metadata = []
        self.api.queue_response(200, content=data)
        identifier = self._identifier()
        metadata = self.api.bibliographic_lookup(identifier)
        eq_("The Incense Game", metadata.title)

    def test_bad_response_raises_exception(self):
        self.api.queue_response(500, content="oops")
        identifier = self._identifier()
        assert_raises_regexp(
            BadResponseException, 
            ".*Got status code 500.*",
            self.api.bibliographic_lookup, identifier
        )

    def test_put_request(self):
        """This is a basic test to make sure the method calls line up
        right--there are more thorough tests in the circulation
        manager, which actually uses this functionality.
        """
        self.api.queue_response(200, content="ok, you put something")
        response = self.api.request('checkout', "put this!", method="PUT")

        # The PUT request went through to the correct URL and the right
        # payload was sent.
        [[method, url, args, kwargs]] = self.api.requests
        eq_("PUT", method)
        eq_(self.api.full_url("checkout"), url)
        eq_('put this!', kwargs['data'])

        # The response is what we'd expect.
        eq_(200, response.status_code)
        eq_("ok, you put something", response.content)


class TestItemListParser(BaseThreeMTest):

    def test_parse_author_string(cls):
        authors = list(ItemListParser.contributors_from_string(
            "Walsh, Jill Paton; Sayers, Dorothy L."))
        eq_([x.sort_name for x in authors], 
            ["Walsh, Jill Paton", "Sayers, Dorothy L."]
        )
        eq_([x.roles for x in authors],
            [[Contributor.AUTHOR_ROLE], [Contributor.AUTHOR_ROLE]]
        )

        [author] = ItemListParser.contributors_from_string(
            "Baum, Frank L. (Frank Lyell)")
        eq_("Baum, Frank L.", author.sort_name)

    def test_parse_genre_string(self):
        def f(genre_string):
            genres = ItemListParser.parse_genre_string(genre_string)
            assert all([x.type == Subject.THREEM for x in genres])
            return [x.identifier for x in genres]

        eq_(["Children's Health", "Health"], 
            f("Children&amp;#39;s Health,Health,"))
        
        eq_(["Action & Adventure", "Science Fiction", "Fantasy", "Magic",
             "Renaissance"], 
            f("Action &amp;amp; Adventure,Science Fiction, Fantasy, Magic,Renaissance,"))


    def test_item_list(cls):
        data = cls.get_data("item_metadata_list_mini.xml")        
        data = list(ItemListParser().parse(data))

        # There should be 2 items in the list.
        eq_(2, len(data))

        cooked = data[0]

        eq_("The Incense Game", cooked.title)
        eq_("A Novel of Feudal Japan", cooked.subtitle)
        eq_("eng", cooked.language)
        eq_("St. Martin's Press", cooked.publisher)
        eq_(datetime.datetime(year=2012, month=9, day=17), 
            cooked.published
        )

        primary = cooked.primary_identifier
        eq_("ddf4gr9", primary.identifier)
        eq_(Identifier.THREEM_ID, primary.type)

        identifiers = sorted(
            cooked.identifiers, key=lambda x: x.identifier
        )
        eq_([u'9781250015280', u'9781250031112', u'ddf4gr9'], 
            [x.identifier for x in identifiers])

        [author] = cooked.contributors
        eq_("Rowland, Laura Joh", author.sort_name)
        eq_([Contributor.AUTHOR_ROLE], author.roles)

        subjects = [x.identifier for x in cooked.subjects]
        eq_(["Children's Health", "Mystery & Detective"], sorted(subjects))

        [pages] = cooked.measurements
        eq_(Measurement.PAGE_COUNT, pages.quantity_measured)
        eq_(304, pages.value)

        [alternate, image, description] = sorted(
            cooked.links, key = lambda x: x.rel)
        eq_("alternate", alternate.rel)
        assert alternate.href.startswith("http://ebook.3m.com/library")

        eq_(Hyperlink.IMAGE, image.rel)
        assert image.href.startswith("http://ebook.3m.com/delivery")

        eq_(Hyperlink.DESCRIPTION, description.rel)
        assert description.content.startswith("<b>Winner")


class TestBibliographicCoverageProvider(TestThreeMAPI):

    def test_process_item_creates_presentation_ready_work(self):
        data = self.get_data("item_metadata_single.xml")
        self.api.queue_response(200, content=data)

        identifier = self._identifier(identifier_type=Identifier.THREEM_ID)
        identifier.identifier = 'ddf4gr9'

        # This book has no LicensePool.
        eq_(None, identifier.licensed_through)

        # Run it through the ThreeMBibliographicCoverageProvider
        provider = ThreeMBibliographicCoverageProvider(
            self._db, threem_api=self.api
        )
        [result] = provider.process_batch([identifier])
        eq_(identifier, result)

        # A LicensePool was created, not because we know anything
        # about how we've licensed this book, but to have a place to
        # store the information about what formats the book is
        # available in.
        pool = identifier.licensed_through
        eq_(0, pool.licenses_owned)
        [lpdm] = pool.delivery_mechanisms
        eq_(
            'application/epub+zip (vnd.adobe/adept+xml)', 
            lpdm.delivery_mechanism.name
        )

        # A Work was created and made presentation ready.
        eq_("The Incense Game", pool.work.title)
        eq_(True, pool.work.presentation_ready)
       
