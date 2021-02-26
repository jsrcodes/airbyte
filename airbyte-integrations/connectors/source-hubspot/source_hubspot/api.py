"""
MIT License

Copyright (c) 2020 Airbyte

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import sys
import time

import backoff
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Callable, Iterable, Iterator, List, Mapping, Optional, Union

import pendulum as pendulum
import requests
from base_python.entrypoint import logger
from source_hubspot.errors import HubspotInvalidAuth, HubspotSourceUnavailable, HubspotRateLimited


def retry_after_handler(**kwargs):
    """Retry helper when we hit the call limit, sleeps for specific duration"""

    def sleep_on_ratelimit(_details):
        _, exc, _ = sys.exc_info()
        if isinstance(exc, HubspotRateLimited):
            retry_after = int(exc.response.headers["Retry-After"])
            logger.info(f"Rate limit reached. Sleeping for {retry_after} seconds")
            time.sleep(retry_after + 1)  # extra second to cover any fractions of second

    def log_giveup(_details):
        logger.error("Max retry limit reached")

    return backoff.on_exception(
        backoff.constant,
        HubspotRateLimited,
        jitter=None,
        on_backoff=sleep_on_ratelimit,
        on_giveup=log_giveup,
        interval=0,  # skip waiting part, we will wait in on_backoff handler
        **kwargs,
    )


class API:
    BASE_URL = "https://api.hubapi.com"
    USER_AGENT = "Airbyte"

    def __init__(self, credentials: Mapping[str, Any]):
        self._credentials = {**credentials}
        self._session = requests.Session()
        self._session.headers = {
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT,
        }

    def _acquire_access_token_from_refresh_token(self):
        payload = {
            "grant_type": "refresh_token",
            "redirect_uri": self._credentials["redirect_uri"],
            "refresh_token": self._credentials["refresh_token"],
            "client_id": self._credentials["client_id"],
            "client_secret": self._credentials["client_secret"],
        }

        resp = requests.post(self.BASE_URL + "/oauth/v1/token", data=payload)
        if resp.status_code == 403:
            raise HubspotInvalidAuth(resp.content, response=resp)

        resp.raise_for_status()
        auth = resp.json()
        self._credentials["access_token"] = auth["access_token"]
        self._credentials["refresh_token"] = auth["refresh_token"]
        self._credentials["token_expires"] = datetime.utcnow() + timedelta(seconds=auth["expires_in"] - 600)
        logger.info("Token refreshed. Expires at %s", self._credentials["token_expires"])

    @property
    def api_key(self) -> Optional[str]:
        """Get API Key if set"""
        return self._credentials.get("api_key")

    @property
    def access_token(self) -> Optional[str]:
        """Get Access Token if set, refreshes token if needed"""
        if not self._credentials.get("access_token"):
            return None

        if self._credentials["token_expires"] is None or self._credentials["token_expires"] < datetime.utcnow():
            self._acquire_access_token_from_refresh_token()
        return self._credentials.get("access_token")

    def _add_auth(self, params: Mapping[str, Any] = None) -> Mapping[str, Any]:
        """Add auth info to request params/header"""
        params = params or {}

        if self.api_key:
            params["hapikey"] = self.api_key
        else:
            self._session.headers["Authorization"] = f"Bearer {self.access_token}"

        return params

    @staticmethod
    def _parse_and_handle_errors(response) -> Mapping[str, Any]:
        """Handle response"""
        if response.status_code == 403:
            raise HubspotSourceUnavailable(response.content)
        elif response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise HubspotRateLimited(
                f"429 Rate Limit Exceeded: API rate-limit has been reached until {retry_after} seconds."
                " See https://developers.hubspot.com/docs/api/usage-details",
                response=response,
            )
        else:
            response.raise_for_status()

        return response.json()

    @retry_after_handler(max_tries=3)
    def get(self, url: str, params=None) -> Union[Mapping[str, Any], List[Mapping[str, Any]]]:
        response = self._session.get(self.BASE_URL + url, params=self._add_auth(params))
        return self._parse_and_handle_errors(response)

    def post(self, url: str, data: Mapping[str, Any], params=None) -> Union[Mapping[str, Any], List[Mapping[str, Any]]]:
        response = self._session.post(self.BASE_URL + url, params=self._add_auth(params), json=data)
        return self._parse_and_handle_errors(response)


class StreamAPI(ABC):
    entity = None

    more_key = None
    data_path = "results"

    page_filter = "offset"
    page_field = "offset"

    chunk_size = 1000 * 60 * 60 * 24  # TODO: use interval
    limit = 100

    def __init__(self, api: API, start_date: str = None, **kwargs):
        self._api: API = api
        self._start_date = pendulum.parse(start_date)

    @abstractmethod
    def list(self, fields) -> Iterable:
        pass

    def read(self, getter: Callable, params: Mapping[str, Any] = None) -> Iterator:
        default_params = {"limit": self.limit, "properties": ",".join(self.properties.keys())}

        params = {**default_params, **params} if params else {**default_params}

        while True:
            response = getter(params=params)
            if response.get(self.data_path) is None:
                raise RuntimeError("Unexpected API response: {} not in {}".format(self.data_path, response.keys()))

            for row in response[self.data_path]:
                yield row

            # pagination
            if "paging" in response:  # APIv3 pagination
                if "next" in response["paging"]:
                    params["after"] = response["paging"]["next"]["after"]
                else:
                    break
            else:
                if not response.get(self.more_key, False):
                    break
                if self.page_field in response:
                    params[self.page_filter] = response[self.page_field]

    def read_chunked(self, getter: Callable, params: Mapping[str, Any] = None):
        params = {**params} if params else {}
        now_ts = int(pendulum.now().timestamp() * 1000)
        start_ts = int(self._start_date.timestamp() * 1000)

        for ts in range(start_ts, now_ts, self.chunk_size):
            end_ts = ts + self.chunk_size
            params["startTimestamp"] = ts
            params["endTimestamp"] = end_ts
            logger.info(f"Reading chunk from {ts} to {end_ts}")
            yield from self.read(getter, params)

    @property
    def properties(self) -> Mapping[str, Any]:
        if not self.entity:
            return {}

        props = {}
        data = self._api.get(f"/properties/v2/{self.entity}/properties")
        for row in data:
            props[row["name"]] = {"type": row["type"]}

        return props


class CRMObjectsAPI(StreamAPI, ABC):
    data_path = "results"

    @property
    @abstractmethod
    def url(self):
        """Endpoint URL"""

    def __init__(self, include_archived_only=False, **kwargs):
        super().__init__(**kwargs)
        self._include_archived_only = include_archived_only

    def list(self, fields) -> Iterable:
        params = {
            "archived": str(self._include_archived_only).lower(),
        }
        for record in self.read(partial(self._api.get, url=self.url), params):
            yield record


class CampaignsAPI(StreamAPI):
    entity = "campaign"
    more_key = "hasMore"
    data_path = "campaigns"
    limit = 500

    def list(self, fields) -> Iterable:
        url = "/email/public/v1/campaigns/by-id"
        for row in self.read(getter=partial(self._api.get, url=url)):
            record = self._api.get(f"/email/public/v1/campaigns/{row['id']}")
            yield record


class CompaniesAPI(CRMObjectsAPI):
    entity = "company"
    url = "/crm/v3/objects/companies"


class ContactListsAPI(CRMObjectsAPI):
    url = "/crm/v3/objects/contacts"


class ContactsAPI(CRMObjectsAPI):
    entity = "contact"
    url = "/crm/v3/objects/contacts"


class DealsAPI(CRMObjectsAPI):
    entity = "deal"
    url = "/crm/v3/objects/deals"


class LineItemsAPI(CRMObjectsAPI):
    entity = "line_item"
    url = "/crm/v3/objects/line_items"


class ProductsAPI(CRMObjectsAPI):
    entity = "product"
    url = "/crm/v3/objects/products"


class QuotesAPI(CRMObjectsAPI):
    entity = "quotes"
    url = "/crm/v3/objects/quotes"


class TicketsAPI(CRMObjectsAPI):
    entity = "ticket"
    url = "/crm/v3/objects/tickets"


class ContactsByCompanyAPI(StreamAPI):
    def list(self, fields) -> Iterable:
        companies_api = CompaniesAPI(api=self._api, start_date=str(self._start_date))
        for company in companies_api.list(fields={}):
            yield from self._contacts_by_company(company["id"])

    def _contacts_by_company(self, company_id):
        url = "/companies/v2/companies/{pk}/vids".format(pk=company_id)
        # FIXME: check if pagination is possible
        params = {"count": 100}
        path = "vids"
        data = self._api.get(url, params)

        if data.get(path) is None:
            raise RuntimeError("Unexpected API response: {} not in {}".format(path, data.keys()))

        for row in data[path]:
            yield {
                "company-id": company_id,
                "contact-id": row,
            }


class DealPipelinesAPI(StreamAPI):
    def list(self, fields) -> Iterable:
        yield from self._api.get("/deals/v1/pipelines")


class EmailEventsAPI(StreamAPI):
    data_path = "events"
    more_key = "hasMore"
    limit = 1000

    def list(self, fields) -> Iterable:
        url = "/email/public/v1/events"

        yield from self.read_chunked(partial(self._api.get, url=url))


class EngagementsAPI(StreamAPI):
    entity = "engagement"
    data_path = "results"
    more_key = "hasMore"
    limit = 250

    def list(self, fields) -> Iterable:
        url = "/engagements/v1/engagements/paged"
        for record in self.read(partial(self._api.get, url=url)):
            record["engagement_id"] = record["engagement"]["id"]
            yield record


class FormsAPI(StreamAPI):
    entity = "form"

    def list(self, fields) -> Iterable:
        for row in self._api.get("/forms/v2/forms"):
            yield row


class OwnersAPI(StreamAPI):
    url = "/crm/v3/owners"
    data_path = "results"

    def list(self, fields) -> Iterable:
        yield from self.read(partial(self._api.get, url=self.url))


class SubscriptionChangesAPI(StreamAPI):
    url = "/email/public/v1/subscriptions/timeline"
    data_path = "timeline"
    more_key = "hasMore"
    limit = 1000

    def list(self, fields) -> Iterable:
        yield from self.read_chunked(partial(self._api.get, url=self.url))


class WorkflowsAPI(StreamAPI):
    def list(self, fields) -> Iterable:
        data = self._api.get("/automation/v3/workflows")
        for record in data["workflows"]:
            yield record
