"""A small, polite HTTP client shared by every source.

Both sources are public government services, so we identify ourselves,
wait between requests, and back off on errors instead of hammering them.
"""

import time

import requests

USER_AGENT = "saratoga-property-scraper (personal home search; github.com/bradenkh/property-scraper)"


class PoliteSession:
    def __init__(self, delay_seconds=1.0, retries=4, timeout=120):
        self.delay_seconds = delay_seconds
        self.retries = retries
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._last_request = 0.0

    def get_json(self, url, params=None):
        for attempt in range(self.retries + 1):
            wait = self.delay_seconds - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                response = self._session.get(url, params=params, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                # ArcGIS reports errors with HTTP 200 and an "error" body.
                if isinstance(payload, dict) and "error" in payload:
                    raise requests.HTTPError(str(payload["error"]))
                return payload
            except (requests.RequestException, ValueError):
                if attempt == self.retries:
                    raise
                time.sleep(2 ** (attempt + 1))
