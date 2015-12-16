import datetime

from nose.tools import (
    eq_,
    set_trace,
    assert_raises,
)

from psycopg2.extras import NumericRange

from . import (
    DatabaseTest,
)

import classifier
from classifier import (
    Classifier,
)

from lane import (
    Facets,
    Pagination,
    Lane,
    LaneList,
    UndefinedLane,
)

from config import (
    Configuration, 
    temp_config,
)

from model import (
    get_one_or_create,
    DataSource,
    Genre,
    Work,
    LicensePool,
    Edition,
    SessionManager,
    WorkGenre,
)


class TestFacets(object):

    def test_facet_groups(self):

        facets = Facets(
            Facets.COLLECTION_MAIN, Facets.AVAILABLE_ALL, Facets.ORDER_TITLE
        )
        all_groups = list(facets.facet_groups)

        # By default, there are a 9 facet transitions: three groups of three.
        eq_(9, len(all_groups))

        # available=all, collection=main, and order=title are the selected
        # facets.
        selected = sorted([x[:2] for x in all_groups if x[-1] == True])
        eq_(
            [('available', 'all'), ('collection', 'main'), ('order', 'title')],
            selected
        )

        test_facet_policy = {
            "enabled" : {
                Facets.ORDER_FACET_GROUP_NAME : [
                    Facets.ORDER_WORK_ID, Facets.ORDER_TITLE
                ],
                Facets.COLLECTION_FACET_GROUP_NAME : [Facets.COLLECTION_FULL],
                Facets.AVAILABILITY_FACET_GROUP_NAME : [Facets.AVAILABLE_ALL],
            },
            "default" : {
                Facets.ORDER_FACET_GROUP_NAME : Facets.ORDER_TITLE,
                Facets.COLLECTION_FACET_GROUP_NAME : Facets.COLLECTION_FULL,
                Facets.AVAILABILITY_FACET_GROUP_NAME : Facets.AVAILABLE_ALL,
            }
        }
        with temp_config() as config:
            config['policies'][Configuration.FACET_POLICY] = test_facet_policy
            facets = Facets(None, None, Facets.ORDER_TITLE)
            all_groups = list(facets.facet_groups)

            # We have disabled almost all the facets, so the list of
            # facet transitions includes only two items.
            #
            # 'Sort by title' was selected, and it shows up as the selected
            # item in this facet group.
            expect = [['order', 'title', True], ['order', 'work_id', False]]
            eq_(expect, sorted([list(x[:2]) + [x[-1]] for x in all_groups]))


    def test_order_facet_to_database_field(self):
        from model import (
            MaterializedWork as mw,
            MaterializedWorkWithGenre as mwg,
        )

        def fields(facet):
            return [
                Facets.order_facet_to_database_field(facet, w, e)
                for w, e in ((Work, Edition), (mw, mw), (mwg, mwg))
            ]

        # You can sort by title...
        eq_([Edition.sort_title, mw.sort_title, mwg.sort_title],
            fields(Facets.ORDER_TITLE))

        # ...by author...
        eq_([Edition.sort_author, mw.sort_author, mwg.sort_author],
            fields(Facets.ORDER_AUTHOR))

        # ...by work ID...
        eq_([Work.id, mw.works_id, mwg.works_id],
            fields(Facets.ORDER_WORK_ID))

        # ...by last update time...
        eq_([Work.last_update_time, mw.last_update_time, mwg.last_update_time],
            fields(Facets.ORDER_LAST_UPDATE))

        # ...by most recently added...
        eq_([LicensePool.availability_time] * 3,
            fields(Facets.ORDER_ADDED_TO_COLLECTION))

        # ...or randomly.
        eq_([Work.random, mw.random, mwg.random],
            fields(Facets.ORDER_RANDOM))

    def test_order_by(self):
        from model import (
            MaterializedWork as mw,
            MaterializedWorkWithGenre as mwg,
        )

        def order(facet, work, edition, ascending=None):
            f = Facets(
                collection=Facets.COLLECTION_FULL, 
                availability=Facets.AVAILABLE_ALL,
                order=facet,
                order_ascending=ascending,
            )
            return f.order_by(work, edition)[0]

        def compare(a, b):
            assert(len(a) == len(b))
            for i in range(0, len(a)):
                print "Trying field #%s" % i
                assert(a[i].compare(b[i]))

        expect = [Edition.sort_author.asc(), Edition.sort_title.asc(), Work.id.asc()]
        actual = order(Facets.ORDER_AUTHOR, Work, Edition, True)  
        compare(expect, actual)

        expect = [Edition.sort_author.desc(), Edition.sort_title.desc(), Work.id.desc()]
        actual = order(Facets.ORDER_AUTHOR, Work, Edition, False)  
        compare(expect, actual)

        expect = [mw.sort_title.asc(), mw.sort_author.asc(), mw.works_id.asc()]
        actual = order(Facets.ORDER_TITLE, mw, mw, True)
        compare(expect, actual)

        expect = [mwg.works_id.asc(), mwg.sort_title.asc(), mwg.sort_author.asc()]
        actual = order(Facets.ORDER_WORK_ID, mwg, mwg, True)
        compare(expect, actual)

        expect = [Work.last_update_time.asc(), Edition.sort_title.asc(), Edition.sort_author.asc(), Work.id.asc()]
        actual = order(Facets.ORDER_LAST_UPDATE, Work, Edition, True)
        compare(expect, actual)

        expect = [mw.random.asc(), mw.sort_title.asc(), mw.sort_author.asc(),
                  mw.works_id.asc()]
        actual = order(Facets.ORDER_RANDOM, mw, mw, True)
        compare(expect, actual)

        expect = [LicensePool.availability_time.desc(), Edition.sort_title.desc(), Edition.sort_author.desc(), Work.id.desc()]
        actual = order(Facets.ORDER_ADDED_TO_COLLECTION, Work, Edition, None)  
        compare(expect, actual)


class TestFacetsApply(DatabaseTest):

    def test_apply(self):
        # Set up works that are matched by different types of collections.

        # A high-quality open-access work.
        open_access_high = self._work(with_open_access_download=True)
        open_access_high.quality = 0.8
        open_access_high.random = 0.2
        
        # A low-quality open-access work.
        open_access_low = self._work(with_open_access_download=True)
        open_access_low.quality = 0.2
        open_access_low.random = 0.4

        # A high-quality licensed work which is not currently available.
        (licensed_e1, licensed_p1) = self._edition(
            data_source_name=DataSource.OVERDRIVE,
            with_license_pool=True)
        licensed_high = self._work(primary_edition=licensed_e1)
        licensed_high.license_pools.append(licensed_p1)
        licensed_high.quality = 0.8
        licensed_p1.open_access = False
        licensed_p1.licenses_owned = 1
        licensed_p1.licenses_available = 0
        licensed_high.random = 0.3

        # A low-quality licensed work which is currently available.
        (licensed_e2, licensed_p2) = self._edition(
            data_source_name=DataSource.OVERDRIVE,
            with_license_pool=True)
        licensed_p2.open_access = False
        licensed_low = self._work(primary_edition=licensed_e2)
        licensed_low.license_pools.append(licensed_p2)
        licensed_low.quality = 0.2
        licensed_p2.licenses_owned = 1
        licensed_p2.licenses_available = 1
        licensed_low.random = 0.1

        qu = self._db.query(Work).join(Work.primary_edition).join(
            Work.license_pools
        )
        def facetify(collection=Facets.COLLECTION_FULL, 
                     available=Facets.AVAILABLE_ALL,
                     order=Facets.ORDER_TITLE
        ):
            f = Facets(collection, available, order)
            return f.apply(self._db, qu)

        # When holds are allowed, we can find all works by asking
        # for everything.
        with temp_config() as config:
            config['policies'][Configuration.HOLD_POLICY] = Configuration.HOLD_POLICY_ALLOW

            everything = facetify()
            eq_(4, everything.count())

        # If we disallow holds, we lose one book even when we ask for
        # everything.
        with temp_config() as config:
            config['policies'][Configuration.HOLD_POLICY] = Configuration.HOLD_POLICY_HIDE
            everything = facetify()
            eq_(3, everything.count())
            assert licensed_high not in everything

        with temp_config() as config:
            config['policies'][Configuration.HOLD_POLICY] = Configuration.HOLD_POLICY_ALLOW
            # Even when holds are allowed, if we restrict to books
            # currently available we lose the unavailable book.
            available_now = facetify(available=Facets.AVAILABLE_NOW)
            eq_(3, available_now.count())
            assert licensed_high not in available_now

            # If we restrict to open-access books we lose two books.
            open_access = facetify(available=Facets.AVAILABLE_OPEN_ACCESS)
            eq_(2, open_access.count())
            assert licensed_high not in open_access
            assert licensed_low not in open_access

            # If we restrict to the main collection we lose the low-quality
            # open-access book.
            main_collection = facetify(collection=Facets.COLLECTION_MAIN)
            eq_(3, main_collection.count())
            assert open_access_low not in main_collection

            # If we restrict to the featured collection we lose both
            # low-quality books.
            featured_collection = facetify(collection=Facets.COLLECTION_FEATURED)
            eq_(2, featured_collection.count())
            assert open_access_low not in featured_collection
            assert licensed_low not in featured_collection

            title_order = facetify(order=Facets.ORDER_TITLE)
            eq_([open_access_high, open_access_low, licensed_high, licensed_low],
                title_order.all())

            random_order = facetify(order=Facets.ORDER_RANDOM)
            eq_([licensed_low, open_access_high, licensed_high, open_access_low],
                random_order.all())


class TestLanes(DatabaseTest):

    def test_all_matching_genres(self):
        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        cooking, ig = Genre.lookup(self._db, classifier.Cooking)
        matches = Lane.all_matching_genres([fantasy, cooking])
        names = sorted([x.name for x in matches])
        eq_(
            [u'Cooking', u'Epic Fantasy', u'Fantasy', u'Historical Fantasy', 
             u'Urban Fantasy'], 
            names
        )

    def test_nonexistent_list_raises_exception(self):
        assert_raises(
            UndefinedLane, Lane, self._db, 
            u"This Will Fail", list_identifier=u"No Such List"
        )

    def test_staff_picks_and_best_sellers_sublane(self):
        staff_picks, ignore = self._customlist(
            foreign_identifier=u"Staff Picks", name=u"Staff Picks!", 
            data_source_name=DataSource.LIBRARY_STAFF,
            num_entries=0
        )
        best_sellers, ignore = self._customlist(
            foreign_identifier=u"NYT Best Sellers", name=u"Best Sellers!", 
            data_source_name=DataSource.NYT,
            num_entries=0
        )
        lane = Lane(
            self._db, "Everything", 
            include_staff_picks=True, include_best_sellers=True
        )

        # A staff picks sublane and a best-sellers sublane have been
        # created for us.
        best, picks = lane.sublanes.lanes
        eq_("Best Sellers", best.display_name)
        eq_("Everything - Best Sellers", best.name)
        eq_(DataSource.NYT, best.list_data_source.name)

        eq_("Staff Picks", picks.display_name)
        eq_("Everything - Staff Picks", picks.name)
        eq_([staff_picks], picks.lists)

    def test_gather_matching_genres(self):
        self.fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        self.urban_fantasy, ig = Genre.lookup(
            self._db, classifier.Urban_Fantasy
        )

        self.cooking, ig = Genre.lookup(self._db, classifier.Cooking)
        self.history, ig = Genre.lookup(self._db, classifier.History)

        # Fantasy contains three subgenres and is restricted to fiction.
        fantasy, default = Lane.gather_matching_genres(
            [self.fantasy], Lane.FICTION_DEFAULT_FOR_GENRE
        )
        eq_(4, len(fantasy))
        eq_(True, default)

        fantasy, default = Lane.gather_matching_genres([self.fantasy], True)
        eq_(4, len(fantasy))
        eq_(True, default)

        fantasy, default = Lane.gather_matching_genres(
            [self.fantasy], True, [self.urban_fantasy]
        )
        eq_(3, len(fantasy))
        eq_(True, default)


        # Attempting to create a contradiction (like nonfiction fantasy)
        # will create a lane broad enough to actually contain books
        fantasy, default = Lane.gather_matching_genres([self.fantasy], False)
        eq_(4, len(fantasy))
        eq_(Lane.BOTH_FICTION_AND_NONFICTION, default)

        # Fantasy and history have conflicting fiction defaults, so
        # although we can make a lane that contains both, we can't
        # have it use the default value.
        assert_raises(UndefinedLane, Lane.gather_matching_genres,
            [self.fantasy, self.history], Lane.FICTION_DEFAULT_FOR_GENRE
        )

    def test_subgenres_become_sublanes(self):
        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        lane = Lane(
            self._db, "YA Fantasy", genres=fantasy, 
            languages='eng',
            audiences=Lane.AUDIENCE_YOUNG_ADULT,
            age_range=[15,16],
            subgenre_behavior=Lane.IN_SUBLANES
        )
        sublanes = lane.sublanes.lanes
        names = sorted([x.name for x in sublanes])
        eq_(["Epic Fantasy", "Historical Fantasy", "Urban Fantasy"],
            names)

        # Sublanes inherit settings from their parent.
        assert all([x.languages==['eng'] for x in sublanes])
        assert all([x.age_range==[15, 16] for x in sublanes])
        assert all([x.audiences==set(['Young Adult']) for x in sublanes])

    def test_custom_sublanes(self):
        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        urban_fantasy, ig = Genre.lookup(self._db, classifier.Urban_Fantasy)

        urban_fantasy_lane = Lane(
            self._db, "Urban Fantasy", genres=urban_fantasy)

        fantasy_lane = Lane(
            self._db, "Fantasy", fantasy, 
            genres=fantasy,
            subgenre_behavior=Lane.IN_SAME_LANE,
            sublanes=[urban_fantasy_lane]
        )
        eq_([urban_fantasy_lane], fantasy_lane.sublanes.lanes)

        # You can just give the name of a genre as a sublane and it
        # will work.
        fantasy_lane = Lane(
            self._db, "Fantasy", fantasy, 
            genres=fantasy,
            subgenre_behavior=Lane.IN_SAME_LANE,
            sublanes="Urban Fantasy"
        )
        eq_([[urban_fantasy]], [x.genres for x in fantasy_lane.sublanes.lanes])



    def test_custom_lanes_conflict_with_subgenre_sublanes(self):

        fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        urban_fantasy, ig = Genre.lookup(self._db, classifier.Urban_Fantasy)

        urban_fantasy_lane = Lane(
            self._db, "Urban Fantasy", genres=urban_fantasy)

        assert_raises(UndefinedLane, Lane,
            self._db, "Fantasy", fantasy, 
            genres=fantasy,
            audiences=Lane.AUDIENCE_YOUNG_ADULT,
            subgenre_behavior=Lane.IN_SUBLANES,
            sublanes=[urban_fantasy_lane]
        )


class TestLanesQuery(DatabaseTest):

    def setup(self):
        super(TestLanesQuery, self).setup()

        # Look up the Fantasy genre and some of its subgenres.
        self.fantasy, ig = Genre.lookup(self._db, classifier.Fantasy)
        self.epic_fantasy, ig = Genre.lookup(self._db, classifier.Epic_Fantasy)
        self.urban_fantasy, ig = Genre.lookup(
            self._db, classifier.Urban_Fantasy
        )

        # Look up the History genre and some of its subgenres.
        self.history, ig = Genre.lookup(self._db, classifier.History)
        self.african_history, ig = Genre.lookup(
            self._db, classifier.African_History
        )

        self.adult_works = {}
        self.ya_works = {}
        self.childrens_works = {}

        for genre in (self.fantasy, self.epic_fantasy, self.urban_fantasy,
                      self.history, self.african_history):
            fiction = True
            if genre in (self.history, self.african_history):
                fiction = False

            # Create a number of books for each genre.
            adult_work = self._work(
                title="%s Adult" % genre.name, 
                audience=Lane.AUDIENCE_ADULT,
                fiction=fiction,
                with_license_pool=True,
                genre=genre,
            )
            self.adult_works[genre] = adult_work
            adult_work.simple_opds_entry = '<entry>'

            # Childrens and YA books need to be attached to a data
            # source other than Gutenberg, or they'll get filtered
            # out.
            ya_edition, lp = self._edition(
                title="%s YA" % genre.name,                 
                data_source_name=DataSource.OVERDRIVE,
                with_license_pool=True
            )
            ya_work = self._work(
                audience=Lane.AUDIENCE_YOUNG_ADULT,
                fiction=fiction,
                with_license_pool=True,
                primary_edition=ya_edition,
                genre=genre,
            )
            self.ya_works[genre] = ya_work
            ya_work.simple_opds_entry = '<entry>'

            childrens_edition, lp = self._edition(
                title="%s Childrens" % genre.name,
                data_source_name=DataSource.OVERDRIVE, with_license_pool=True
            )
            childrens_work = self._work(
                audience=Lane.AUDIENCE_CHILDREN,
                fiction=fiction,
                with_license_pool=True,
                primary_edition=childrens_edition,
                genre=genre,
            )
            if genre == self.epic_fantasy:
                childrens_work.target_age = NumericRange(7, 9, '[]')
            else:
                childrens_work.target_age = NumericRange(8, 10, '[]')
            self.childrens_works[genre] = childrens_work
            childrens_work.simple_opds_entry = '<entry>'

        # Create generic 'Adults Only' fiction and nonfiction books
        # that are not in any genre.
        self.nonfiction = self._work(
            title="Generic Nonfiction", fiction=False,
            audience=Lane.AUDIENCE_ADULTS_ONLY,
            with_license_pool=True
        )
        self.nonfiction.simple_opds_entry = '<entry>'
        self.fiction = self._work(
            title="Generic Fiction", fiction=True,
            audience=Lane.AUDIENCE_ADULTS_ONLY,
            with_license_pool=True
        )
        self.fiction.simple_opds_entry = '<entry>'

        # Create a work of music.
        self.music = self._work(
            title="Music", fiction=False,
            audience=Lane.AUDIENCE_ADULT,
            with_license_pool=True,
        )
        self.music.primary_edition.medium=Edition.MUSIC_MEDIUM
        self.music.simple_opds_entry = '<entry>'

        # Create a Spanish book.
        self.spanish = self._work(
            title="Spanish book", fiction=True,
            audience=Lane.AUDIENCE_ADULT,
            with_license_pool=True,
            language='spa'
        )
        self.spanish.simple_opds_entry = '<entry>'

        # Refresh the materialized views so that all these books are present
        # in the 
        SessionManager.refresh_materialized_views(self._db)

    def test_lanes(self):
        # I'm putting all these tests into one method because the
        # setup function is so very expensive.

        def test_expectations(lane, expected_count, predicate,
                              mw_predicate=None):
            """Ensure that a database query and a query of the
            materialized view give the same results.
            """
            mw_predicate = mw_predicate or predicate
            w = lane.works().all()
            mw = lane.materialized_works().all()
            eq_(len(w), expected_count)
            eq_(len(mw), expected_count)
            assert all([predicate(x) for x in w])
            assert all([mw_predicate(x) for x in mw])
            return w, mw

        # The 'everything' lane contains 18 works -- everything except
        # the music.
        lane = Lane(self._db, "Everything")
        w, mw = test_expectations(lane, 18, lambda x: True)

        # The 'Spanish' lane contains 1 book.
        lane = Lane(self._db, "Spanish", languages='spa')
        eq_(['spa'], lane.languages)
        w, mw = test_expectations(lane, 1, lambda x: True)
        eq_([self.spanish], w)

        # The 'everything except English' lane contains that same book.
        lane = Lane(self._db, "Not English", exclude_languages='eng')
        eq_(None, lane.languages)
        eq_(['eng'], lane.exclude_languages)
        w, mw = test_expectations(lane, 1, lambda x: True)
        eq_([self.spanish], w)

        # The 'music' lane contains 1 work of music
        lane = Lane(self._db, "Music", media=Edition.MUSIC_MEDIUM)
        w, mw = test_expectations(
            lane, 1, 
            lambda x: x.primary_edition.medium==Edition.MUSIC_MEDIUM,
            lambda x: x.medium==Edition.MUSIC_MEDIUM
        )
        
        # The 'English fiction' lane contains ten fiction books.
        lane = Lane(self._db, "English Fiction", fiction=True, languages='eng')
        w, mw = test_expectations(
            lane, 10, lambda x: x.fiction
        )

        # The 'nonfiction' lane contains seven nonfiction books.
        # It does not contain the music.
        lane = Lane(self._db, "Nonfiction", fiction=False)
        w, mw = test_expectations(
            lane, 7, 
            lambda x: x.primary_edition.medium==Edition.BOOK_MEDIUM and not x.fiction,
            lambda x: x.medium==Edition.BOOK_MEDIUM and not x.fiction
        )

        # The 'adults' lane contains five books for adults.
        lane = Lane(self._db, "Adult English",
                    audiences=Lane.AUDIENCE_ADULT, languages='eng')
        w, mw = test_expectations(
            lane, 5, lambda x: x.audience==Lane.AUDIENCE_ADULT
        )

        # This lane contains those five books plus two adults-only
        # books.
        audiences = [Lane.AUDIENCE_ADULT, Lane.AUDIENCE_ADULTS_ONLY]
        lane = Lane(self._db, "Adult + Adult Only",
                    audiences=audiences, languages='eng'
        )
        w, mw = test_expectations(
            lane, 7, lambda x: x.audience in audiences
        )
        assert(2, len([x for x in w if x.audience==Lane.AUDIENCE_ADULTS_ONLY]))
        assert(2, len([x for x in mw if x.audience==Lane.AUDIENCE_ADULTS_ONLY]))

        # The 'Young Adults' lane contains five books.
        lane = Lane(self._db, "Young Adults", 
                    audiences=Lane.AUDIENCE_YOUNG_ADULT)
        w, mw = test_expectations(
            lane, 5, lambda x: x.audience==Lane.AUDIENCE_YOUNG_ADULT
        )

        # There is one book suitable for seven-year-olds.
        lane = Lane(
            self._db, "If You're Seven", audiences=Lane.AUDIENCE_CHILDREN,
            age_range=7
        )
        w, mw = test_expectations(
            lane, 1, lambda x: x.audience==Lane.AUDIENCE_CHILDREN
        )

        # There are four books suitable for ages 10-12.
        lane = Lane(
            self._db, "10-12", audiences=Lane.AUDIENCE_CHILDREN,
            age_range=(10,12)
        )
        w, mw = test_expectations(
            lane, 4, lambda x: x.audience==Lane.AUDIENCE_CHILDREN
        )
       
        #
        # Now let's start messing with genres.
        #

        # Here's an 'adult fantasy' lane, in which the subgenres of Fantasy
        # are kept in the same place as generic Fantasy.
        lane = Lane(
            self._db, "Adult Fantasy",
            genres=[self.fantasy], 
            subgenre_behavior=Lane.IN_SAME_LANE,
            fiction=Lane.FICTION_DEFAULT_FOR_GENRE,
            audiences=Lane.AUDIENCE_ADULT,
        )
        # We get three books: Fantasy, Urban Fantasy, and Epic Fantasy.
        w, mw = test_expectations(
            lane, 3, lambda x: True
        )
        expect = [u'Epic Fantasy Adult', u'Fantasy Adult', u'Urban Fantasy Adult']
        eq_(expect, sorted([x.sort_title for x in w]))
        eq_(expect, sorted([x.sort_title for x in mw]))

        # Here's a 'YA fantasy' lane in which urban fantasy is explicitly
        # excluded (maybe because it has its own separate lane).
        lane = Lane(
            self._db, full_name="Adult Fantasy",
            genres=[self.fantasy], 
            exclude_genres=[self.urban_fantasy],
            subgenre_behavior=Lane.IN_SAME_LANE,
            fiction=Lane.FICTION_DEFAULT_FOR_GENRE,
            audiences=Lane.AUDIENCE_YOUNG_ADULT,
        )

        # Urban Fantasy does not show up in this lane's genres.
        eq_(["Epic Fantasy", "Fantasy", "Historical Fantasy"], 
            sorted([x.name for x in lane.genres]))

        # We get two books: Fantasy and Epic Fantasy.
        w, mw = test_expectations(
            lane, 2, lambda x: True
        )
        expect = [u'Epic Fantasy YA', u'Fantasy YA']
        eq_(expect, sorted([x.sort_title for x in w]))
        eq_(sorted([x.id for x in w]), sorted([x.works_id for x in mw]))

        # Finally, test lanes based on lists. Create two lists, each
        # with one book.
        one_day_ago = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        one_year_ago = datetime.datetime.utcnow() - datetime.timedelta(days=365)

        fic_name = "Best Sellers - Fiction"
        best_seller_list_1, ignore = self._customlist(
            foreign_identifier=fic_name, name=fic_name,
            num_entries=0
        )
        best_seller_list_1.add_entry(
            self.fiction.primary_edition, first_appearance=one_day_ago
        )
        
        nonfic_name = "Best Sellers - Nonfiction"
        best_seller_list_2, ignore = self._customlist(
            foreign_identifier=nonfic_name, name=nonfic_name, num_entries=0
        )
        best_seller_list_2.add_entry(
            self.nonfiction.primary_edition, first_appearance=one_year_ago
        )

        # Create a lane for one specific list
        fiction_best_sellers = Lane(
            self._db, full_name="Fiction Best Sellers",
            list_identifier=fic_name
        )
        w, mw = test_expectations(
            fiction_best_sellers, 1, 
            lambda x: x.sort_title == self.fiction.sort_title
        )

        # Create a lane for all best-sellers.
        all_best_sellers = Lane(
            self._db, full_name="All Best Sellers",
            list_data_source=best_seller_list_1.data_source.name
        )
        w, mw = test_expectations(
            all_best_sellers, 2, 
            lambda x: x.sort_title in (
                self.fiction.sort_title, self.nonfiction.sort_title
            )
        )

        # Combine list membership with another criteria (nonfiction)
        all_nonfiction_best_sellers = Lane(
            self._db, full_name="All Nonfiction Best Sellers",
            fiction=False,
            list_data_source=best_seller_list_1.data_source.name
        )
        w, mw = test_expectations(
            all_nonfiction_best_sellers, 1, 
            lambda x: x.sort_title==self.nonfiction.sort_title
        )

        # Apply a cutoff date to a best-seller list,
        # excluding the work that was last seen a year ago.
        best_sellers_past_week = Lane(
            self._db, full_name="Best Sellers - The Past Week",
            list_data_source=best_seller_list_1.data_source.name,
            list_seen_in_previous_days=7
        )
        w, mw = test_expectations(
            best_sellers_past_week, 1, 
            lambda x: x.sort_title==self.fiction.sort_title
        )
   
    def test_from_description(self):
        """Create a LaneList from a simple description."""
        lanes = LaneList.from_description(
            self._db,
            None,
            [dict(
                full_name="Fiction",
                fiction=True,
                audiences=Classifier.AUDIENCE_ADULT,
            ),
             classifier.Fantasy,
             dict(
                 full_name="Young Adult",
                 fiction=Lane.BOTH_FICTION_AND_NONFICTION,
                 audiences=Classifier.AUDIENCE_YOUNG_ADULT,
             ),
         ]
        )

        fantasy_genre, ignore = Genre.lookup(self._db, classifier.Fantasy.name)
        urban_fantasy_genre, ignore = Genre.lookup(self._db, classifier.Urban_Fantasy.name)

        fiction = lanes.by_languages['']['Fiction']
        young_adult = lanes.by_languages['']['Young Adult']
        fantasy = lanes.by_languages['']['Fantasy'] 
        urban_fantasy = lanes.by_languages['']['Urban Fantasy'] 

        eq_(set([fantasy, fiction, young_adult]), set(lanes.lanes))

        eq_("Fiction", fiction.name)
        eq_(set([Classifier.AUDIENCE_ADULT]), fiction.audiences)
        eq_([], fiction.genres)
        eq_(True, fiction.fiction)

        eq_("Fantasy", fantasy.name)
        eq_(set(), fantasy.audiences)
        eq_(set(fantasy_genre.self_and_subgenres), set(fantasy.genres))
        eq_(True, fantasy.fiction)

        eq_("Urban Fantasy", urban_fantasy.name)
        eq_(set(), urban_fantasy.audiences)
        eq_([urban_fantasy_genre], urban_fantasy.genres)
        eq_(True, urban_fantasy.fiction)

        eq_("Young Adult", young_adult.name)
        eq_(set([Classifier.AUDIENCE_YOUNG_ADULT]), young_adult.audiences)
        eq_([], young_adult.genres)
        eq_(Lane.BOTH_FICTION_AND_NONFICTION, young_adult.fiction)
