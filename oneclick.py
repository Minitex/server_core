from nose.tools import set_trace
import base64
from collections import defaultdict
import datetime
from dateutil.relativedelta import relativedelta
import json
import logging
import os
import re

from config import (
    Configuration, 
    temp_config,
)

from util import LanguageCodes

from util.http import (
    BadResponseException, 
    HTTP,
)
from coverage import CoverageFailure

from model import (
    Contributor,
    DataSource,
    DeliveryMechanism,
    Edition,
    Hyperlink,
    Identifier,
    Representation,
    Subject,
    Work,
)

from metadata_layer import (
    CirculationData,
    ContributorData,
    FormatData,
    IdentifierData,
    LinkData,
    Metadata,
    SubjectData,
)

from config import Configuration
from coverage import BibliographicCoverageProvider

class OneClickAPI(object):

    API_VERSION = "v1"
    DATE_FORMAT = "%Y-%m-%d" #ex: 2013-12-27

    # a complete response returns the json structure with more data fields than a basic response does
    RESPONSE_VERBOSITY = {0:'basic', 1:'compact', 2:'complete', 3:'extended', 4:'hypermedia'}

    log = logging.getLogger("OneClick API")

    def __init__(self, _db, library_id=None, username=None, password=None, 
        remote_stage=None, base_url=None, basic_token=None, 
        ebook_loan_length=None, eaudio_loan_length=None):
        self._db = _db
            
        self.library_id = library_id
        self.username = username
        self.password = password
        self.remote_stage = remote_stage
        self.base_url = base_url or ''
        self.base_url = self.base_url + self.API_VERSION
        self.token = basic_token
        # expiration defaults are OneClick-general
        self.ebook_loan_length = ebook_loan_length
        self.eaudio_loan_length = eaudio_loan_length


    @classmethod
    def create_identifier_strings(cls, identifiers):
        identifier_strings = []
        for i in identifiers:
            if isinstance(i, Identifier):
                value = i.identifier
            else:
                value = i
            identifier_strings.append(value)

        return identifier_strings


    @classmethod
    def from_config(cls, _db):
        config = Configuration.integration(Configuration.ONECLICK_INTEGRATION, required=True)
        property_names = [
            'library_id',
            'username',
            'password',
            'remote_stage', 
            'url', 
            'basic_token', 
            'ebook_loan_length', 
            'eaudio_loan_length'
        ]
        property_values = {}
        for name in property_names:
            value = config.get(name)
            if value:
                value = value.encode("utf8")
            property_values[name] = value

        if len(property_values.values()) == 0:
            cls.log.info("No OneClick client configured.")
            raise ValueError("No OneClick client configured.")


        api = cls(_db, library_id=property_values['library_id'], 
            username=property_values['username'], password=property_values['password'], 
            remote_stage=property_values['remote_stage'], base_url=property_values['url'], 
            basic_token=property_values['basic_token'], 
            ebook_loan_length=property_values['ebook_loan_length'], 
            eaudio_loan_length=property_values['eaudio_loan_length'])

        return api


    @property
    def source(self):
        return DataSource.lookup(self._db, DataSource.ONECLICK)


    @property
    def authorization_headers(self):
        # the token given us by OneClick is already utf/base64-encoded
        authorization = self.token
        return dict(Authorization="Basic " + authorization)


    def _make_request(self, url, method, headers, data=None, params=None, **kwargs):
        """Actually make an HTTP request."""
        return HTTP.request_with_timeout(
            method, url, headers=headers, data=data,
            params=params, **kwargs
        )


    def request(self, url, method='get', extra_headers={}, data=None,
                params=None, verbosity='complete'):
        """Make an HTTP request.
        """
        if verbosity not in self.RESPONSE_VERBOSITY.values():
            verbosity = self.RESPONSE_VERBOSITY[2]

        headers = dict(extra_headers)
        headers['Content-Type'] = 'application/json'
        headers['Accept-Media'] = verbosity
        headers.update(self.authorization_headers)

        # prevent the code throwing a BadResponseException when OneClick 
        # responds with a 500, because OneClick uses 500s to indicate bad input, 
        # rather than server error.
        # must list all 9 possibilities to use
        allowed_response_codes = ['1xx', '2xx', '3xx', '4xx', '5xx', '6xx', '7xx', '8xx', '9xx']
        # for now, do nothing with disallowed error codes, but in the future might have 
        # some that will warrant repeating the request.
        disallowed_response_codes = []

        response = self._make_request(
            url=url, method=method, headers=headers,
            data=data, params=params, 
            allowed_response_codes=allowed_response_codes, 
            disallowed_response_codes=disallowed_response_codes
        )
        
        return response


    ''' --------------------- Getters and Setters -------------------------- '''

    def get_all_available_through_search(self):
        """
        Gets a list of ebook and eaudio items this library has access to, that are currently
        available to lend.  Uses the "availability" facet of the search function.
        An alternative to self.get_availability_info().
        Calls paged search until done.
        Uses minimal verbosity for result set.

        Note:  Some libraries can see other libraries' catalogs, even if the patron 
        cannot checkout the items.  The library ownership information is in the "interest" 
        fields of the response.

        :return A dictionary representation of the response, containing catalog count and ebook item - interest pairs.
        """
        page = 0;
        response = self.search(availability='available', verbosity=self.RESPONSE_VERBOSITY[0])

        try: 
            respdict = response.json()
        except Exception, e:
            raise BadResponseException("availability_search", "OneClick availability response not parseable.")

        if not respdict:
            raise BadResponseException("availability_search", "OneClick availability response not parseable - has no structure.")

        if not ('pageIndex' in respdict and 'pageCount' in respdict):
            raise BadResponseException("availability_search", "OneClick availability response not parseable - has no page counts.")

        page_index = respdict['pageIndex']
        page_count = respdict['pageCount']

        while (page_count > (page_index+1)):
            page_index += 1
            response = self.search(availability='available', verbosity=self.RESPONSE_VERBOSITY[0], page_index=page_index)
            tempdict = response.json()
            if not ('items' in tempdict):
                raise BadResponseException("availability_search", "OneClick availability response not parseable - has no next dict.")
            item_interest_pairs = tempdict['items']
            respdict['items'].extend(item_interest_pairs)

        return respdict


    def get_all_catalog(self): 
        """
        Gets the entire OneClick catalog for a particular library.

        Note:  This call taxes OneClick's servers, and is to be performed sparingly.
        The results are returned unpaged.

        Also, the endpoint returns about as much metadata per item as the media/{isbn} endpoint does.  
        If want more metadata, perform a search.

        :return A list of dictionaries representation of the response.
        """
        url = "%s/libraries/%s/media/all" % (self.base_url, str(self.library_id))

        response = self.request(url)

        try:
            resplist = response.json()
        except Exception, e:
            raise BadResponseException(url, "OneClick all catalog response not parseable.")

        return response.json()


    def get_delta(self, from_date=None, to_date=None, verbosity=None): 
        """
        Gets the changes to the library's catalog.

        Note:  As of now, OneClick saves deltas for past 6 months, and can display them  
        in max 2-month increments. 

        :return A dictionary listing items added/removed/modified in the collection.
        """
        url = "%s/libraries/%s/media/delta" % (self.base_url, str(self.library_id))

        today = datetime.datetime.now()
        two_months = datetime.timedelta(days=60)
        six_months = datetime.timedelta(days=180)

        # from_date must be real, and less than 6 months ago
        if from_date and isinstance(from_date, basestring):
            from_date = datetime.datetime.strptime(from_date[:10], self.DATE_FORMAT)
            if (from_date > today) or ((today-from_date) > six_months):
                raise ValueError("from_date %s must be real, in the past, and less than 6 months ago." % from_date)

        # to_date must be real, and not in the future or too far in the past
        if to_date and isinstance(to_date, basestring):
            to_date = datetime.datetime.strptime(to_date[:10], self.DATE_FORMAT)
            if (to_date > today) or ((today - to_date) > six_months):
                raise ValueError("to_date %s must be real, and neither in the future nor too far in the past." % to_date)

        # can't reverse time direction
        if from_date and to_date and (from_date > to_date):
            raise ValueError("from_date %s cannot be after to_date %s." % (from_date, to_date))

        # can request no more that two month date range for catalog delta
        if from_date and to_date and ((to_date - from_date) > two_months):
            raise ValueError("from_date %s - to_date %s asks for too-wide date range." % (from_date, to_date))

        if from_date and not to_date:
            to_date = from_date + two_months
            if to_date > today:
                to_date = today

        if to_date and not from_date:
            from_date = to_date - two_months
            if from_date < today - six_months:
                from_date = today - six_months

        if not from_date and not to_date:
            from_date = today - two_months
            to_date = today

        args = dict()
        args['begin'] = from_date
        args['end'] = to_date

        response = self.request(url, params=args, verbosity=verbosity)
        return response.json()


    def get_ebook_availability_info(self, media_type='ebook'):
        """
        Gets a list of ebook items this library has access to, through the "availability" endpoint.
        The response at this endpoint is laconic -- just enough fields per item to 
        identify the item and declare it either available to lend or not.

        :param media_type 'ebook'/'eaudio'

        :return A list of dictionary items, each item giving "yes/no" answer on a book's current availability to lend.
        Example of returned item format:
            "timeStamp": "2016-10-07T16:11:52.5887333Z"
            "isbn": "9781420128567"
            "mediaType": "eBook"
            "availability": false
            "titleId": 39764
        """
        url = "%s/libraries/%s/media/%s/availability" % (self.base_url, str(self.library_id), media_type) 

        response = self.request(url)

        try:
            resplist = response.json()
        except Exception, e:
            raise BadResponseException(url, "OneClick availability response not parseable.")

        if not resplist:
            raise IOError("OneClick availability response not parseable - has no resplist.")

        return resplist


    def get_metadata_by_isbn(self, identifier):
        """
        Gets metadata, s.a. publisher, date published, genres, etc for the 
        ebook or eaudio item passed, using isbn to search on. 
        If isbn is not found, the response we get from OneClick is an error message, 
        and we throw an error.

        :return the json dictionary of the response object
        """
        if not identifier:
            raise ValueError("Need valid identifier to get metadata.")

        identifier_string = self.create_identifier_strings([identifier])[0]
        url = "%s/libraries/%s/media/%s" % (self.base_url, str(self.library_id), identifier_string) 

        response = self.request(url)

        try:
            respdict = response.json()
        except Exception, e:
            raise BadResponseException(url, "OneClick isbn search response not parseable.")

        if not respdict:
            # should never happen
            raise BadResponseException(url, "OneClick isbn search response not parseable - has no respdict.")

        if "message" in respdict:
            message = respdict['message']
            if (message.startswith("Invalid 'MediaType', 'TitleId' or 'ISBN' token value supplied: ") or 
                message.startswith("eXtensible Framework was unable to locate the resource")):
                # we searched for item that's not in library's catalog -- a mistake, but not an exception
                return None
            else:
                # something more serious went wrong
                error_message = "get_metadata_by_isbn(%s) in library #%s catalog ran into problems: %s" % (identifier_string, str(self.library_id), error_message)
                raise BadResponseException(url, message)

        return respdict


    def populate_all_catalog(self):
        """ Call get_all_catalog to get all of library's book info from OneClick.
        Create Work, Edition, LicensePool objects in our database.
        """
        catalog_list = self.get_all_catalog()
        items_transmitted = len(catalog_list)
        items_created = 0
        for catalog_item in catalog_list:
            result = self.update_metadata(catalog_item)
            if result:
                items_created += 1

        return items_transmitted, items_created


    def populate_delta(self, months=1):
        """ Call get_delta for the last month to get all of the library's book info changes 
        from OneClick.  Update Work, Edition, LicensePool objects in our database.
        """
        today = datetime.datetime.now()
        time_ago = relativedelta(months=months)

        delta = self.get_delta(from_date=(today - time_ago), to_date=today)
        if not delta or len(delta) < 1:
            return None, None

        items_added = delta[0].get("addedTitles", None)
        items_removed = delta[0].get("removedTitles", None)

        items_transmitted = len(items_added) + len(items_removed)
        items_updated = 0
        for catalog_item in items_added:
            result = self.update_metadata(catalog_item)
            if result:
                items_updated += 1

        for catalog_item in items_removed:
            metadata = OneClickRepresentationExtractor.isbn_info_to_metadata(catalog_item)

            if not metadata:
                # generate a CoverageFailure to let the system know to revisit this book
                # TODO:  if did not create a Work, but have a CoverageFailure for the isbn, 
                # check that re-processing that coverage would generate the work.
                e = "Could not extract metadata from OneClick data: %r" % catalog_item
                make_note = CoverageFailure(identifier, e, data_source=self.output_source, transient=True)

            # convert IdentifierData into Identifier, if can
            identifier, made_new = metadata.primary_identifier.load(_db=self._db)
            if identifier and not made_new:
                [work] = Work.from_identifiers(self._db, [identifier])
                if work:
                    for cr in work.coverage_records:
                        self._db.delete(cr)
                    for pool in work.license_pools:
                        if pool.presentation_edition:
                            for contribution in pool.presentation_edition.contributions:
                                self._db.delete(contribution)
                            self._db.delete(pool.presentation_edition.contributions[0])
                        for mechanism in pool.delivery_mechanisms:
                            self._db.delete(mechanism)
                        self._db.delete(pool)
                    self._db.delete(work)
                    items_updated += 1

        return items_transmitted, items_updated


    def update_metadata(self, catalog_item):
        metadata = OneClickRepresentationExtractor.isbn_info_to_metadata(catalog_item)

        if not metadata:
            # generate a CoverageFailure to let the system know to revisit this book
            # TODO:  if did not create a Work, but have a CoverageFailure for the isbn, 
            # check that re-processing that coverage would generate the work.
            e = "Could not extract metadata from OneClick data: %r" % catalog_item
            make_note = CoverageFailure(identifier, e, data_source=self.output_source, transient=True)

        # make a database object
        coverage_provider = OneClickBibliographicCoverageProvider(_db=self._db)
        # convert IdentifierData into Identifier, if can
        identifier, made_new = metadata.primary_identifier.load(_db=self._db)
        if identifier:
            result = coverage_provider.set_metadata(
                identifier, metadata, metadata_replacement_policy=None
            )
            return result

        return None


    def search(self, mediatype='ebook', genres=[], audience=None, availability=None, author=None, title=None, 
        page_size=100, page_index=None, verbosity=None): 
        """
        Form a rest-ful search query, send to OneClick, and obtain the results.

        :param mediatype Facet to limit results by media type.  Options are: "eaudio", "ebook".
        :param genres The books found lie at intersection of genres passed.
        :audience Facet to limit results by target age group.  Options include (there may be more): "adult", 
            "beginning-reader", "childrens", "young-adult".
        :param availability Facet to limit results by copies left.  Options are "available", "unavailable", or None
        :param author Full name to search on.
        :param author Book title to search on.
        :param page_index Used for paginated result sets.  Zero-based.
        :param verbosity "basic" returns smaller number of response json lines than "complete", etc..

        :return the response object
        """
        url = "%s/libraries/%s/search" % (self.base_url, str(self.library_id))

        # make sure availability is in allowed format
        if availability not in ("available", "unavailable"):
            availability = None

        args = dict()
        if mediatype:
            args['media-type'] = mediatype
        if genres:
            args['genre'] = genres
        if audience:
            args['audience'] = audience
        if availability:
            args['availability'] = availability
        if author:
            args['author'] = author
        if title:
            args['title'] = title
        if page_size != 100:
            args['page-size'] = page_size
        if page_index:
            args['page-index'] = page_index

        response = self.request(url, params=args, verbosity=verbosity)
        return response



class MockOneClickAPI(OneClickAPI):

    def __init__(self, _db, with_token=True, *args, **kwargs):
        with temp_config() as config:
            config[Configuration.INTEGRATIONS]['OneClick'] = {
                'library_id' : 'library_id_123',
                'username' : 'username_123',
                'password' : 'password_123',
                'remote_stage' : 'qa', 
                'base_url' : 'www.oneclickapi.test', 
                'basic_token' : 'abcdef123hijklm', 
                "ebook_loan_length" : '21', 
                "eaudio_loan_length" : '21'
            }
            super(MockOneClickAPI, self).__init__(_db, 
                library_id='library_id_123', 
                username='username_123', password='password_123', 
                remote_stage='qa', base_url='www.oneclickapi.test', 
                basic_token='abcdef123hijklm', 
                ebook_loan_length='21', 
                eaudio_loan_length='21')

        if with_token:
            self.token = "mock token"

        self.responses = []
        self.requests = []


    def queue_response(self, status_code, headers={}, content=None):
        from testing import MockRequestsResponse
        self.responses.insert(
            0, MockRequestsResponse(status_code, headers, content)
        )


    def _make_request(self, url, *args, **kwargs):
        self.requests.append([url, args, kwargs])
        response = self.responses.pop()
        return HTTP._process_response(
            url, response, kwargs.get('allowed_response_codes'),
            kwargs.get('disallowed_response_codes')
        )



class OneClickRepresentationExtractor(object):
    """ Extract useful information from OneClick's JSON representations. """
    DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ" #ex: 2013-12-27T00:00:00Z
    DATE_FORMAT = "%Y-%m-%d" #ex: 2013-12-27

    log = logging.getLogger("OneClick representation extractor")

    oneclick_formats = {
        "ebook-epub-oneclick" : (
            Representation.EPUB_MEDIA_TYPE, DeliveryMechanism.ADOBE_DRM
        ),
        "audiobook-mp3-oneclick" : (
            "vnd.librarysimplified/obfuscated-one-click", DeliveryMechanism.ONECLICK_DRM
        ),
        "audiobook-mp3-open" : (
            "audio/mpeg3", DeliveryMechanism.NO_DRM
        ),
    }

    oneclick_medium_to_simplified_medium = {
        "eBook" : Edition.BOOK_MEDIUM,
        "eAudio" : Edition.AUDIO_MEDIUM,
    }


    @classmethod
    def image_link_to_linkdata(cls, link_url, rel):
        if not link_url or (link_url.find("http") < 0):
            return None

        media_type = None
        if link_url.endswith(".jpg"):
            media_type = "image/jpeg"

        return LinkData(rel=rel, href=link_url, media_type=media_type)


    @classmethod
    def isbn_info_to_metadata(cls, book, include_bibliographic=True, include_formats=True):
        """Turn OneClick's JSON representation of a book into a Metadata object.
        Assumes the JSON is in the format that comes from the media/{isbn} endpoint.

        TODO:  Use the seriesTotal field.

        :param book a json response-derived dictionary of book attributes
        """
        if not 'isbn' in book:
            return None
        oneclick_id = book['isbn']
        primary_identifier = IdentifierData(
            Identifier.ONECLICK_ID, oneclick_id
        )

        metadata = Metadata(
            data_source=DataSource.ONECLICK,
            primary_identifier=primary_identifier,
        )

        if include_bibliographic:
            title = book.get('title', None)
            # NOTE: An item that's part of a series, will have the seriesName field, and 
            # will have its seriesPosition and seriesTotal fields set to >0.
            # An item not part of a series will have the seriesPosition and seriesTotal fields 
            # set to 0, and will not have a seriesName at all.
            # Sometimes, series position and total == 0, for many series items (ex: "seriesName": "EngLits").
            # Sometimes, seriesName is set to "Default Blank", meaning "not actually a series".
            series_name = book.get('seriesName', None)

            series_position = book.get('seriesPosition', None)
            if series_position:
                try:
                    series_position = int(series_position)
                except ValueError:
                    # not big enough deal to stop the whole process
                    series_position = None

            # ignored for now
            series_total = book.get('seriesTotal', None)
            # ignored for now
            has_digital_rights = book.get('hasDigitalRights', None)

            publisher = book.get('publisher', None)
            if 'publicationDate' in book:
                published = datetime.datetime.strptime(
                    book['publicationDate'][:10], cls.DATE_FORMAT)
            else:
                published = None

            if 'language' in book:
                language = LanguageCodes.string_to_alpha_3(book['language'])
            else:
                language = 'eng'

            contributors = []
            if 'authors' in book:
                authors = book['authors']
                for author in authors.split(";"):
                    sort_name = author.strip()
                    roles = [Contributor.AUTHOR_ROLE]
                    contributor = ContributorData(sort_name=sort_name, roles=roles)
                    contributors.append(contributor)

            if 'narrators' in book:
                narrators = book['narrators']
                for narrator in narrators.split(";"):
                    sort_name = narrator.strip()
                    roles = [Contributor.NARRATOR_ROLE]
                    contributor = ContributorData(sort_name=sort_name, roles=roles)
                    contributors.append(contributor)

            subjects = []
            if 'genres' in book:
                # example: "FICTION / Humorous / General"
                genres = book['genres']
                subject = SubjectData(
                    type=Subject.BISAC, identifier=genres,
                    weight=100
                )
                subjects.append(subject)

            if 'primaryGenre' in book:
                # example: "humorous-fiction,mystery,womens-fiction"
                genres = book['primaryGenre']
                for genre in genres.split(","):
                    subject = SubjectData(
                        type=Subject.ONECLICK, identifier=genre.strip(),
                        weight=100
                    )
                    subjects.append(subject)

            # audience options are: adult, beginning-reader, childrens, young-adult
            # NOTE: audience can be set to "Adult" while publisher is "HarperTeen".
            audience = book.get('audience', None)
            if audience:
                subject = SubjectData(
                    type=Subject.ONECLICK_AUDIENCE,
                    identifier=audience.strip().lower(),
                    weight=10
                )
                subjects.append(subject)

            # options are: "eBook", "eAudio"
            oneclick_medium = book.get('mediaType', None)
            if oneclick_medium and oneclick_medium not in cls.oneclick_medium_to_simplified_medium:
                cls.log.error(
                    "Could not process medium %s for %s", oneclick_medium, oneclick_id)
                
            medium = cls.oneclick_medium_to_simplified_medium.get(
                oneclick_medium, Edition.BOOK_MEDIUM
            )

            identifiers = [IdentifierData(Identifier.ONECLICK_ID, oneclick_id, 1)]
            
            links = []
            # A cover and its thumbnail become a single LinkData.
            # images come in small (ex: 71x108px), medium (ex: 95x140px), 
            # and large (ex: 128x192px) sizes
            if 'images' in book:
                images = book['images']
                for image in images:
                    if image['name'] == "large":
                        image_data = cls.image_link_to_linkdata(image['url'], Hyperlink.IMAGE)
                    if image['name'] == "medium":
                        thumbnail_data = cls.image_link_to_linkdata(image['url'], Hyperlink.THUMBNAIL_IMAGE)
                    if image['name'] == "small":
                        thumbnail_data_backup = cls.image_link_to_linkdata(image['url'], Hyperlink.THUMBNAIL_IMAGE)

                if not thumbnail_data and thumbnail_data_backup:
                    thumbnail_data = thumbnail_data_backup

                if image_data:
                    if thumbnail_data:
                        image_data.thumbnail = thumbnail_data
                    links.append(image_data)


            # Descriptions become links.
            description = book.get('description', None)
            if description:
                links.append(
                    LinkData(
                        # there can be fuller descriptions in the search endpoint output
                        rel=Hyperlink.SHORT_DESCRIPTION,
                        content=description,
                        media_type="text/html",
                    )
                )

            metadata.title = title
            metadata.language = language
            metadata.medium = medium
            metadata.series = series_name
            metadata.series_position = series_position
            metadata.publisher = publisher
            metadata.published = published
            metadata.identifiers = identifiers
            metadata.subjects = subjects
            metadata.contributors = contributors
            metadata.links = links

        if include_formats:
            formats = []
            if metadata.medium == Edition.BOOK_MEDIUM:
                content_type, drm_scheme = cls.oneclick_formats.get("ebook-epub-oneclick")
                formats.append(FormatData(content_type, drm_scheme))
            elif metadata.medium == Edition.AUDIO_MEDIUM:
                content_type, drm_scheme = cls.oneclick_formats.get("audiobook-mp3-oneclick")
                formats.append(FormatData(content_type, drm_scheme))
            else:
                cls.log.warn("Unfamiliar format: %s", format_id)

            # Make a CirculationData so we can write the formats, 
            circulationdata = CirculationData(
                data_source=DataSource.ONECLICK,
                primary_identifier=primary_identifier,
                formats=formats,
            )

            metadata.circulation = circulationdata

        return metadata



class OneClickBibliographicCoverageProvider(BibliographicCoverageProvider):
    """Fill in bibliographic metadata for OneClick records."""

    def __init__(self, _db, input_identifier_types=None, 
                 metadata_replacement_policy=None, oneclick_api=None,
                 **kwargs):
        # We ignore the value of input_identifier_types, but it's
        # passed in by RunCoverageProviderScript, so we accept it as
        # part of the signature.
        
        oneclick_api = oneclick_api or OneClickAPI(_db)
        super(OneClickBibliographicCoverageProvider, self).__init__(
            _db, oneclick_api, DataSource.ONECLICK,
            batch_size=25, 
            metadata_replacement_policy=metadata_replacement_policy,
            **kwargs
        )


    def process_item(self, identifier):
        """ OneClick availability information is served separately from 
        the book's metadata.  Furthermore, the metadata returned by the 
        "book by isbn" request is less comprehensive than the data returned 
        by the "search titles/genres/etc." endpoint.

        This method hits the "by isbn" endpoint and updates the bibliographic 
        metadata returned by it. 
        """
        try:
            response_dictionary = self.api.get_metadata_by_isbn(identifier)
        except BadResponseException as error:
            return CoverageFailure(identifier, error.message, data_source=self.output_source, transient=True)
        except IOError as error:
            return CoverageFailure(identifier, error.message, data_source=self.output_source, transient=True)

        if not response_dictionary:
            message = "Cannot find OneClick metadata for %r" % identifier
            return CoverageFailure(identifier, message, data_source=self.output_source, transient=True)

        metadata = OneClickRepresentationExtractor.isbn_info_to_metadata(response_dictionary)

        if not metadata:
            e = "Could not extract metadata from OneClick data: %r" % response_dictionary
            return CoverageFailure(identifier, e, data_source=self.output_source, transient=True)

        result = self.set_metadata(
            identifier, metadata, 
            metadata_replacement_policy=self.metadata_replacement_policy
        )

        if not isinstance(result, CoverageFailure):
            self.handle_success(identifier)

        return result







