import logging
import urllib.parse

import requests
import time
from retry.api import retry_call

from datetime import datetime

from github import Github

from typing import Optional, List, Optional, Dict

from pydantic import BaseModel

from snyk import SnykClient

from time import sleep

from __version__ import __version__

logger = logging.getLogger(__name__)


class RateLimit:
    def __init__(self, gh: Github, pages):
        self.core_limit = gh.get_rate_limit().core.limit
        self.search_limit = gh.get_rate_limit().search.limit
        # we want to know how many calls had been made before we created this object
        # calling this a tare
        self.core_tare = gh.get_rate_limit().core.limit - gh.get_rate_limit().core.remaining
        self.search_tare = gh.get_rate_limit().search.limit - gh.get_rate_limit().search.remaining
        self.gh = gh
        self.core_calls = [0]
        self.search_calls = [0]
        self.repo_count = 0
        self.pages = pages

    def update(self, display: bool = False):
        core_call = self.core_limit - self.core_tare - self.gh.get_rate_limit().core.remaining
        search_call = self.search_limit - self.search_tare - self.gh.get_rate_limit().search.remaining

        self.core_calls.append(core_call)
        self.search_calls.append(search_call)

        if display is True:
            core_diff = self.core_calls[-1] - self.core_calls[-2]
            search_diff = self.search_calls[-1] - self.search_calls[-2]
            print(f"GH RateLimit: Core Calls = {core_diff}")
            print(f"GH RateLimit: Search Calls = {search_diff}")

    def add_calls(self, repo_total: int):
        self.repo_count = repo_total

    def check(self, kind="core"):

        if kind == "core":
            rl = self.gh.get_rate_limit().core
        elif kind == "search":
            rl = self.gh.get_rate_limit().search

        expiration = rl.reset

        now = datetime.utcnow()

        reset_countdown = expiration - now

        remaining = rl.remaining

        needed_requests = (self.repo_count // self.pages) + 1

        if needed_requests > remaining:
            print(f"\n{needed_requests} requests needed and {remaining} remaining")
            print(f"Sleeping: {reset_countdown.seconds} seconds")
            time.sleep(int(reset_countdown.seconds))

    def total(self):
        print(f"GH RateLimit: Total Core Calls = {self.core_calls[-1]}")
        print(f"GH RateLimit: Total Search Calls = {self.search_calls[-1]}")


class V3Projects(BaseModel):
    pass


class V3Targets(BaseModel):
    pass


class V3Target(BaseModel):
    pass


class SnykV3Client(object):
    API_URL = "https://api.snyk.io/v3"
    V3_VERS = "2021-08-20~beta"
    USER_AGENT = f"pysnyk/snyk_services/sync/{__version__}"

    def __init__(
        self,
        token: str,
        url: Optional[str] = None,
        version: Optional[str] = None,
        user_agent: Optional[str] = USER_AGENT,
        debug: bool = False,
        tries: int = 1,
        delay: int = 1,
        backoff: int = 2,
    ):
        self.api_token = token
        self.api_url = url or self.API_URL
        self.api_vers = version or self.V3_VERS
        self.api_headers = {
            "Authorization": "token %s" % self.api_token,
            "User-Agent": user_agent,
        }
        self.api_post_headers = self.api_headers
        self.api_post_headers["Content-Type"] = "Content-Type: application/vnd.api+json; charset=utf-8"
        self.tries = tries
        self.backoff = backoff
        self.delay = delay

    def request(
        self,
        method,
        url: str,
        headers: object,
        params: object = None,
        json: object = None,
    ) -> requests.Response:

        resp: requests.Response

        resp = method(
            url,
            json=json,
            params=params,
            headers=headers,
        )

        if resp.status_code == 429:
            logger.debug("RESP: %s" % resp.headers)
            print("Hit 429 Timeout, Sleeping 60s before erroring out")
            sleep(60)
            resp.raise_for_status()
        elif not resp or resp.status_code >= requests.codes.server_error:
            logger.debug("RESP: %s" % resp.headers)
            resp.raise_for_status()

        return resp

    def get(self, path: str, params: dict = {}) -> requests.Response:

        # path = ensure_version(path, self.V3_VERS)
        path = cleanup_path(path)

        if "version" not in params.keys():
            params["version"] = self.V3_VERS

        params = {k: v for (k, v) in params.items() if v}

        # because python bool(True) != javascript bool(True) - True vs true
        for k, v in params.items():
            if isinstance(v, bool):
                params[k] = str(v).lower()

        url = self.api_url + path
        logger.debug("GET: %s" % url)
        resp = retry_call(
            self.request,
            fargs=[requests.get, url, self.api_headers, params],
            tries=self.tries,
            delay=self.delay,
            backoff=self.backoff,
            logger=logger,
        )

        if not resp.ok:
            resp.raise_for_status()

        logger.debug("RESP: %s" % resp.headers)

        return resp

    def get_all_pages(self, path: str, params: dict = {}) -> List:
        """
        This is a wrapper of .get() that assumes we're going to get paginated results.
        In that case we really just want concated lists from each pages 'data'
        """

        # this is a raw primative but a higher level module might want something that does an
        # arbitrary path + origin=foo + limit=100 url construction instead before being sent here

        limit = params["limit"]

        data = list()

        page = self.get(path, params).json()

        data.extend(page["data"])

        while "next" in page["links"].keys():
            next_url = urllib.parse.urlsplit(page["links"]["next"])
            query = urllib.parse.parse_qs(next_url.query)

            for k, v in query.items():
                params[k] = v

            params["limit"] = limit

            page = self.get(next_url.path, params).json()
            data.extend(page["data"])

        return data


def cleanup_path(path: str):
    if path[0] != "/":
        return f"/{path}"
    else:
        return path


def cleanup_url(path: str):
    if "https://app.snyk.io/api/v1/" in path:
        path = path.replace("https://app.snyk.io/api/v1/", "")

    return path


def ensure_version(path: str, version: str) -> str:

    query = path.split("/")[-1]

    if "version" in query.lower():
        return path
    else:
        if "?" in query and query[-1] != "&" and query[-1] != "?":
            logger.debug("ensure_version Case 1")
            return f"{path}&version={version}"
        elif query[-1] == "&" or query[-1] == "?":
            logger.debug("ensure_version Case 2")
            return f"{path}version={version}"
        else:
            logger.debug("ensure_version Case 3")
            return f"{path}?version={version}"


def v1_get_pages(
    path: str, v1_client: SnykClient, list_name: str, per_page_key: str = "perPage", per_page_val: int = 100
) -> Dict:
    """
    For paged resources on the v1 api that use links headers
    """

    if path[-1] != "?" and "&" not in path:
        path = f"{path}?"

    path = f"{path}&{per_page_key}={per_page_val}"

    resp = v1_client.get(path)

    page = resp.json()

    return_page = page

    while "next" in resp.links:
        url = resp.links["next"]["url"]

        url = cleanup_url(url)

        resp = v1_client.get(url)

        page = resp.json()

        return_page[list_name].extend(page[list_name])

    return return_page
