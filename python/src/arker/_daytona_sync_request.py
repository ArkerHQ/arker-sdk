"""Synchronous process requests with the native connection/read time budget."""

from contextlib import contextmanager
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

import httpx2 as httpx
import urllib3

from ._request_timeout import request_executor, request_executor_scope


@contextmanager
def sync_request_executor():
    # Preserve an async executor or the enclosing synchronous operation's pools.
    if request_executor() is not None:
        yield
        return
    pools = {}
    ssl_context = httpx.create_ssl_context(verify=True, trust_env=True)

    def execute(method, url, headers, data, timeout):
        target = urlsplit(url)
        proxies = getproxies()
        proxy = None if proxy_bypass(target.hostname or "") else proxies.get(target.scheme, proxies.get("all"))
        if proxy not in pools:
            options = {"ssl_context": ssl_context}
            if proxy:
                address = urlsplit(proxy)
                if address.scheme not in {"http", "https"}:
                    raise httpx.UnsupportedProtocol("Unsupported HTTP proxy scheme")
                if address.username is not None:
                    credentials = unquote(address.username) + ":" + unquote(address.password or "")
                    options["proxy_headers"] = urllib3.make_headers(proxy_basic_auth=credentials)
                    proxy_url = urlunsplit(address._replace(netloc=address.netloc.rsplit("@", 1)[-1]))
                else:
                    proxy_url = proxy
                pools[proxy] = urllib3.ProxyManager(proxy_url, **options)
            else:
                pools[proxy] = urllib3.PoolManager(**options)
        seconds = timeout.read if isinstance(timeout, httpx.Timeout) else timeout
        try:
            response = pools[proxy].request(
                method,
                url,
                headers=headers,
                body=data,
                timeout=urllib3.Timeout(total=seconds),
                retries=False,
                redirect=False,
                preload_content=True,
            )
            return response.status, response.data
        except urllib3.exceptions.ConnectTimeoutError as error:
            raise httpx.ConnectTimeout("HTTP connection timeout") from error
        except urllib3.exceptions.TimeoutError as error:
            raise httpx.ReadTimeout("HTTP response timeout") from error
        except urllib3.exceptions.HTTPError as error:
            raise httpx.RequestError("HTTP transport failed") from error

    try:
        with request_executor_scope(execute):
            yield
    finally:
        for pool in pools.values():
            pool.clear()
