import demistomock as demisto
from CommonServerPython import *
from CommonServerUserPython import *

'''IMPORTS'''
from elasticsearch import Elasticsearch, RequestsHttpConnection
from elasticsearch_dsl import Search
from elasticsearch_dsl.query import QueryString
import requests
import warnings

# Disable insecure warnings
requests.packages.urllib3.disable_warnings()
warnings.filterwarnings(action="ignore", message='.*using SSL with verify_certs=False is insecure.')

HTTP_ERRORS = {
    400: '400 Bad Request - Incorrect or invalid parameters',
    401: '401 Unauthorized - Incorrect or invalid username or password',
    403: '403 Forbidden - The account does not support performing this task',
    404: '404 Not Found - Elasticsearch server was not found',
    408: '408 Timeout - Check port number or Elasticsearch server credentials',
    410: '410 Gone - Elasticsearch server no longer exists in the service',
    500: '500 Internal Server Error - Internal error',
    503: '503 Service Unavailable'
}

'''VARIABLES FOR FETCH INDICATORS'''
FETCH_SIZE = 50
MODULE_TO_FEEDMAP_KEY = 'moduleToFeedMap'
FEED_TYPE_GENERIC = 'Generic Feed (fill in configuration below)'
FEED_TYPE_CORTEX = 'Cortex XSOAR Feed'
FEED_TYPE_CORTEX_MT = 'Cortex XSOAR MT Shared Feed'


class ElasticsearchClient:
    def __init__(self, insecure, server, username, password, time_field, time_method, fetch_index, fetch_time):
        self._insecure = insecure
        self._proxy = handle_proxy()
        if not self._proxy:
            self._proxy = None
        self._server = server
        self._http_auth = (username, password) if (username and password) else None

        self.time_field = time_field
        self.time_method = time_method
        self.fetch_index = fetch_index
        self.fetch_time = fetch_time
        self.es = self._elasticsearch_builder()

    def _elasticsearch_builder(self):
        """Builds an Elasticsearch obj with the necessary credentials, proxy settings and secure connection."""
        return Elasticsearch(hosts=[self._server], connection_class=RequestsHttpConnection, http_auth=self._http_auth,
                             verify_certs=self._insecure, proxies=self._proxy)

    def send_test_request(self):
        headers = {
            'Content-Type': "application/json"
        }
        return requests.get(self._server, auth=self._http_auth, verify=self._insecure, headers=headers,
                            proxies=self._proxy)


''' ###################### COMMANDS ###################### '''


def test_command(client, feed_type, src_val, src_type, default_type, time_method, time_field, fetch_time):
    """Test instance was set up correctly"""
    now = datetime.now()
    if feed_type == FEED_TYPE_GENERIC:
        if not src_val:
            return_error('Please provide a "Source Indicator Value"')
        if not src_type and not default_type:
            return_error('Please provide a "Source Indicator Type" or "Default Indicator Type"')
        if not default_type:
            return_error('Please provide a "Default Indicator Type"')
        if not time_method:
            return_error('Please provide a "Time Method"')
        if time_field and not fetch_time:
            return_error('Please provide a "First Fetch Time"')
        get_scan_generic_format(client, now)
    else:
        get_scan_insight_format(client, now, feed_type=feed_type)
    try:
        res = client.send_test_request()
        if res.status_code >= 400:
            try:
                res.raise_for_status()

            except requests.exceptions.HTTPError as e:
                if HTTP_ERRORS.get(res.status_code) is not None:
                    # if it is a known http error - get the message form the preset messages
                    return_error("Failed to connect. "
                                 "The following error occurred: {}".format(HTTP_ERRORS.get(res.status_code)))

                else:
                    # if it is unknown error - get the message from the error itself
                    return_error("Failed to connect. The following error occurred: {}".format(str(e)))

    except requests.exceptions.RequestException as e:
        return_error("Failed to connect. Check Server URL field and port number.\nError message: " + str(e))

    demisto.results('ok')


def get_indicators_command(client, feed_type, src_val, src_type, default_type):
    """Implements es-get-indicators command"""
    now = datetime.now()
    if feed_type == FEED_TYPE_GENERIC:
        search = get_scan_generic_format(client, now)
        get_generic_indicators(search, src_val, src_type, default_type)
    else:
        search = get_scan_insight_format(client, now, feed_type=feed_type)
        get_demisto_indicators(search)


def get_generic_indicators(search, src_val, src_type, default_type):
    """Implements get indicators in generic format"""
    ioc_lst: list = []
    for hit in search.scan():
        hit_lst = extract_indicators_from_generic_hit(hit, src_val, src_type, default_type)
        ioc_lst.extend(hit_lst)
    hr = tableToMarkdown('Indicators', ioc_lst, [src_val])
    return_outputs(hr, {}, ioc_lst)


def get_demisto_indicators(search):
    """Implements get indicators in insight format"""
    limit = int(demisto.args().get('limit', FETCH_SIZE))
    indicators_list: list = []
    ioc_enrch_lst: list = []
    for hit in search.scan():
        hit_lst, hit_enrch_lst = extract_indicators_from_insight_hit(hit)
        indicators_list.extend(hit_lst)
        ioc_enrch_lst.extend(hit_enrch_lst)
        if len(indicators_list) >= limit:
            break
    hr = tableToMarkdown('Indicators', list(set(map(lambda ioc: ioc.get('name'), indicators_list))), 'Name')
    if ioc_enrch_lst:
        for ioc_enrch in ioc_enrch_lst:
            hr += tableToMarkdown('Enrichment', ioc_enrch, ['value', 'sourceBrand', 'score'])
    return_outputs(hr, {}, indicators_list)


def fetch_indicators_command(client, feed_type, src_val, src_type, default_type, last_fetch):
    """Implements fetch-indicators command"""
    last_fetch_timestamp = get_last_fetch_timestamp(last_fetch, client.time_method, client.fetch_time)
    if feed_type:
        now_ts = fetch_and_create_indicators_insight_format(client, last_fetch_timestamp)
    else:
        now_ts = fetch_and_create_indicators_generic_format(client, src_val, src_type, default_type,
                                                            last_fetch_timestamp)
    demisto.setIntegrationContext({'time': now_ts})


def fetch_and_create_indicators_generic_format(client, src_val, src_type, default_type, last_fetch_timestamp):
    """Fetches hits in generic format and then creates indicators from them"""
    now = datetime.now()
    search = get_scan_generic_format(client, now, last_fetch_timestamp)
    ioc_lst: list = []
    for hit in search.scan():
        hit_lst = extract_indicators_from_generic_hit(hit, src_val, src_type, default_type)
        ioc_lst.extend(hit_lst)
    if ioc_lst:
        for b in batch(ioc_lst, batch_size=2000):
            demisto.createIndicators(b)
    return str(now.timestamp())


def get_timestamp_first_fetch(last_fetch, time_method):
    """Gets the last fetch time as a datetime and converts it to the relevant timestamp format"""
    # this theorticly shouldn't happen but just in case
    if str(last_fetch).isdigit():
        return int(last_fetch)

    if time_method == 'Timestamp-Seconds':
        return int(last_fetch.timestamp())

    elif time_method == 'Timestamp-Milliseconds':
        return int(last_fetch.timestamp() * 1000)


def get_last_fetch_timestamp(last_fetch, time_method, fetch_time):
    """Get the last fetch timestamp 11"""
    if last_fetch:
        if 'Simple-Date' == time_method or 'Milliseconds' in time_method:
            last_fetch_timestamp = int(last_fetch) * 1000
        else:
            last_fetch_timestamp = float(last_fetch)  # type: ignore
    else:
        last_fetch_timestamp, _ = parse_date_range(date_range=fetch_time, date_format='%Y-%m-%dT%H:%M:%S.%f', utc=False,
                                                   to_timestamp=True)
        # if timestamp: get the last fetch to the correct format of timestamp
        if time_method != 'Timestamp-Milliseconds':
            last_fetch_timestamp = int(last_fetch_timestamp / 1000)

    return last_fetch_timestamp


def get_scan_generic_format(client, now, last_fetch_timestamp=None):
    """Gets a scan object in generic format"""
    # if method is simple date - convert the date string to datetime
    es = client.es
    time_field = client.time_field
    fetch_index = client.fetch_index
    if not fetch_index:
        fetch_index = '_all'
    if time_field:
        query = QueryString(query=time_field + ':*')
        range_field = {
            time_field: {'gt': datetime.fromtimestamp(last_fetch_timestamp), 'lte': now}} if last_fetch_timestamp else {
            time_field: {'lte': now}}
        search = Search(using=es, index=fetch_index).filter({'range': range_field}).query(query)
    else:
        search = Search(using=es, index=fetch_index).query(QueryString(query="*"))
    return search


def extract_indicators_from_generic_hit(hit, src_val, src_type, default_type):
    """Extracts indicators in generic format"""
    ioc_lst = []
    ioc = hit_to_indicator(hit, src_val, src_type, default_type)
    if ioc.get('value'):
        ioc_lst.append(ioc)
    return ioc_lst


def fetch_and_create_indicators_insight_format(client, last_fetch_timestamp):
    """Fetches hits in insight format and then creates indicators from them"""
    now = datetime.now()
    search = get_scan_insight_format(client, now, last_fetch_timestamp)
    ioc_lst: list = []
    ioc_enrch_lst: list = []
    for hit in search.scan():
        hit_lst, hit_enrch_lst = extract_indicators_from_insight_hit(hit)
        ioc_lst.extend(hit_lst)
        ioc_enrch_lst.extend(hit_enrch_lst)
    if ioc_lst:
        for b in batch(ioc_lst, batch_size=2000):
            demisto.createIndicators(b)
    if ioc_enrch_lst:
        ioc_enrch_batches = create_enrichment_batches(ioc_enrch_lst)
        for enrch_batch in ioc_enrch_batches:
            # ensure batch sizes don't exceed 2000
            for b in batch(enrch_batch, batch_size=2000):
                demisto.createIndicators(b)
    return str(now.timestamp())


def get_scan_insight_format(client, now, last_fetch_timestamp=None, feed_type=None):
    """Gets a scan object in insight format"""
    time_field = client.time_field
    range_field = {
        time_field: {'gt': datetime.fromtimestamp(last_fetch_timestamp), 'lte': now}} if last_fetch_timestamp else {
        time_field: {'lte': now}}
    es = client.es
    query = QueryString(query=time_field + ":*")
    indices = client.fetch_index
    if not indices:
        if feed_type == FEED_TYPE_CORTEX_MT:
            indices = '*-shared*'
            tenant_hash = demisto.getIndexHash()
            if tenant_hash:
                # all shared indexes minus this tenant shared
                indices += f',-*{tenant_hash}*-shared*'
        else:
            indices = '_all'
    search = Search(using=es, index=indices).filter({'range': range_field}).query(query)
    return search


def extract_indicators_from_insight_hit(hit):
    """Extracts indicators from an insight hit including enrichments"""
    ioc_lst = []
    ioc_enirhcment_list = []
    ioc = hit_to_indicator(hit)
    if ioc.get('value'):
        ioc_lst.append(ioc)
        module_to_feedmap = ioc.get(MODULE_TO_FEEDMAP_KEY)
        updated_module_to_feedmap = {}
        if module_to_feedmap:
            ioc_enrichment_obj = []
            for key, val in module_to_feedmap.items():
                if val.get('isEnrichment'):
                    ioc_enrichment_obj.append(val)
                else:
                    updated_module_to_feedmap[key] = val
            if ioc_enrichment_obj:
                ioc_enirhcment_list.append(ioc_enrichment_obj)
            ioc[MODULE_TO_FEEDMAP_KEY] = updated_module_to_feedmap
    return ioc_lst, ioc_enirhcment_list


def hit_to_indicator(hit, ioc_val_key='name', ioc_type_key=None, default_ioc_type=None):
    """Convert a single hit to an indicator"""
    ioc_dict = hit.to_dict()
    ioc_dict['value'] = ioc_dict.get(ioc_val_key)
    ioc_dict['rawJSON'] = dict(ioc_dict)
    if ioc_type_key:
        ioc_dict['type'] = ioc_dict.get(ioc_type_key)
    if not ioc_dict.get('type'):
        ioc_dict['type'] = default_ioc_type
    return ioc_dict


def create_enrichment_batches(ioc_enrch_lst):
    """
    Create batches for enrichments, by separating enrichments that come from the same indicator into diff batches
    """
    max_enrch_len = 0
    for ioc_enrch_obj in ioc_enrch_lst:
        max_enrch_len = max(max_enrch_len, len(ioc_enrch_obj))
    enrch_batch_lst = []
    for i in range(max_enrch_len):
        enrch_batch_obj = []
        for ioc_enrch_obj in ioc_enrch_lst:
            if i < len(ioc_enrch_obj):
                enrch_batch_obj.append(ioc_enrch_obj[i])
        enrch_batch_lst.append(enrch_batch_obj)
    return enrch_batch_lst


def main():
    try:
        LOG('command is %s' % (demisto.command(),))
        params = demisto.params()
        server = params.get('url', '').rstrip('/')
        creds = params.get('credentials')
        username, password = (creds.get('identifier'), creds.get('password')) if creds else (None, None)
        insecure = not params.get('insecure')
        feed_type = params.get('feed_type')
        time_field = params.get('time_field') if feed_type == FEED_TYPE_GENERIC else 'calculatedTime'
        time_method = params.get('time_method')
        fetch_index = params.get('fetch_index')
        fetch_time = demisto.params().get('fetch_time', '3 days')
        client = ElasticsearchClient(insecure, server, username, password, time_field, time_method, fetch_index,
                                     fetch_time)
        src_val = params.get('src_val')
        src_type = params.get('src_type')
        default_type = params.get('default_type')
        last_fetch = demisto.getIntegrationContext().get('time')

        if demisto.command() == 'test-module':
            test_command(client, feed_type, src_val, src_type, default_type, time_method, time_field, fetch_time)
        elif demisto.command() == 'fetch-indicators':
            fetch_indicators_command(client, feed_type, src_val, src_type, default_type, last_fetch)
        elif demisto.command() == 'es-get-indicators':
            get_indicators_command(client, feed_type, src_val, src_type, default_type)
    except Exception as e:
        return_error("Failed executing {}.\nError message: {}".format(demisto.command(), str(e)), error=e)


main()
