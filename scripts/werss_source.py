"""Refresh WeRSS's WeRead source before reading its RSS snapshot."""

import json
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


def refresh_feed(feed, credentials_file, timeout):
    parsed = urlsplit(feed["url"])
    match = re.fullmatch(r"/feed/(MP_WXS_\d+)\.xml", parsed.path)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or not match:
        raise ValueError("WeRSS refresh requires a local MP_WXS RSS URL")
    base = f"{parsed.scheme}://{parsed.netloc}/api/v1/wx"
    credentials = dict(
        line.split("=", 1) for line in credentials_file.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )

    def request(path, data, headers):
        with urlopen(Request(base + path, data=data, headers=headers), timeout=timeout) as response:
            result = json.load(response)
        if result.get("code") != 0:
            # Do not expose upstream error strings, which may contain credentials.
            raise ValueError("WeRSS authentication or article refresh failed")
        return result["data"]

    login = request("/auth/login", urlencode({
        "username": credentials["USERNAME"], "password": credentials["PASSWORD"],
    }).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
    result = request("/weread/collect", json.dumps({
        "mp_id": match[1], "faker_id": match[1],
        "mp_name": feed.get("name", feed["id"]), "gather_content": True,
    }).encode(), {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + login["access_token"],
    })
    if "collected" not in result:
        raise ValueError("WeRSS returned no collection result")
