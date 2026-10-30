"""Synchronous process requests with the native connection/read time budget."""

from contextlib import contextmanager
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

import httpx2 as httpx
import urllib3

from ._compat import import_optional
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
                # Status retries remain owned by the outer API request loop.
                retries=urllib3.Retry(
                    total=3,
                    allowed_methods={"GET"},
                    status=0,
                    redirect=0,
                    raise_on_status=False,
                    respect_retry_after_header=False,
                )
                if method.upper() == "GET"
                else False,
                redirect=False,
                preload_content=False,
            )
            try:
                return response.status, response.data
            finally:
                response.close()
                response.release_conn()
        except urllib3.exceptions.HTTPError as error:
            if method.upper() == "GET":
                # Do not let the outer HTTP loop replay an exhausted request or
                # a response whose headers have already been received.
                errors = import_optional("daytona.common.errors")
                if errors is None:
                    raise RuntimeError("HTTP transport failed") from error
                error_type = errors.DaytonaError
                if isinstance(error, urllib3.exceptions.TimeoutError):
                    error_type = errors.DaytonaConnectionTimeoutError
                elif isinstance(error, urllib3.exceptions.ProtocolError):
                    error_type = errors.DaytonaConnectionError
                raise error_type("HTTP transport failed") from error
            if isinstance(error, urllib3.exceptions.ConnectTimeoutError):
                raise httpx.ConnectTimeout("HTTP connection timeout") from error
            if isinstance(error, urllib3.exceptions.TimeoutError):
                raise httpx.ReadTimeout("HTTP response timeout") from error
            raise httpx.RequestError("HTTP transport failed") from error

    try:
        with request_executor_scope(execute):
            yield
    finally:
        for pool in pools.values():
            pool.clear()
