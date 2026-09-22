"""HTTPS fallback between our two owned service names, without replaying actions.

Only DNS/TCP/TLS connection failures are retried. Once HTTP request bytes may have
been sent, the caller receives the failure: a POST must never execute twice merely
because its reply was lost. No addresses, credentials, or route history are logged.
"""
import http.client
import threading
import urllib.error
import urllib.parse
import urllib.request


_HOSTS = ('play.lightsoutranked.com', 'lightsoutranked.com')
_preferred = _HOSTS[0]
_lock = threading.Lock()
CONNECT_TIMEOUT_SECONDS = 8


def _owned_host(url):
    parts = urllib.parse.urlsplit(url)
    return parts.netloc if parts.scheme == 'https' and parts.netloc in _HOSTS else None


class _ConnectError(OSError):
    """The HTTPS connection failed before an HTTP request could be sent."""


class _HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        read_timeout = self.timeout
        self.timeout = min(read_timeout, CONNECT_TIMEOUT_SECONDS)
        try:
            # The standard implementation verifies the certificate and hostname,
            # including when tunnelling through a configured system proxy.
            super().connect()
        except (OSError, http.client.HTTPException) as error:
            self.close()
            raise _ConnectError('Secure connection unavailable') from error
        finally:
            self.timeout = read_timeout
        self.sock.settimeout(read_timeout)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


class _OwnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials to another service or downgrade to plaintext.
        # Refuse action redirects too: a later connection failure must not retry an
        # action whose first request already reached the server.
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        same_origin = (old.scheme, old.netloc) == (new.scheme, new.netloc)
        owned_pair = _owned_host(req.full_url) and _owned_host(newurl)
        if req.get_method() not in ('GET', 'HEAD') or not (same_origin or owned_pair):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request_on_host(request, host):
    parts = urllib.parse.urlsplit(request.full_url)
    url = urllib.parse.urlunsplit(parts._replace(netloc=host))
    # urllib supplies Host for the actual TLS destination. Never carry a previous
    # Host/proxy credential along when changing destinations.
    headers = {key: value for key, value in request.header_items()
               if key.lower() not in ('host', 'proxy-authorization')}
    return urllib.request.Request(url, data=request.data, headers=headers,
                                  method=request.get_method())


def urlopen(request, timeout=20):
    """Open once, or try the other owned host if connecting failed before send.

    Custom/local/file endpoints stay single-host with the same redirect guard.
    The reachable owned
    host is preferred by subsequent requests in this process only; it is not an IP
    pin, a saved player setting, or a claim that the two routes are independent.
    """
    global _preferred
    if not isinstance(request, urllib.request.Request):
        request = urllib.request.Request(str(request))
    if not _owned_host(request.full_url):
        return urllib.request.build_opener(_OwnedRedirectHandler()).open(request, timeout=timeout)
    # Only bytes/None are replayable bodies. Current JSON callers all use bytes.
    replayable = request.data is None or isinstance(request.data, bytes)
    with _lock:
        first = _preferred
    hosts = (first, next(host for host in _HOSTS if host != first))
    opener = urllib.request.build_opener(_HTTPSHandler(), _OwnedRedirectHandler())
    for index, host in enumerate(hosts):
        try:
            response = opener.open(_request_on_host(request, host), timeout=timeout)
        except urllib.error.HTTPError:
            raise  # A server response is not a failed route.
        except urllib.error.URLError as error:
            if index or not replayable or not isinstance(error.reason, _ConnectError):
                raise
        else:
            with _lock:
                _preferred = _owned_host(response.geturl()) or host
            return response
