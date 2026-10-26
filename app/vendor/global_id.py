"""Language-neutral authority client; never generates IDs locally."""
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def _private_host(host):
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return any(address in ipaddress.ip_network(network) for network in
                   ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', 'fc00::/7', '::1/128'))
    except ValueError:
        host = host.lower()
        return (host == 'localhost' or bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', host))
                or host.endswith('.svc') or host.endswith('.svc.cluster.local'))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GlobalIdClient:
    def __init__(self, config=None):
        self.config = dict(os.environ if config is None else config)
        self.http = urllib.request.build_opener(_NoRedirect)
        self._lock = threading.RLock()
        self._socket = None
        self._socket_token = None
        self._sequence = 0
        self._cached_token = None
        self._expires = 0

    def _post(self, url, data, content_type, token=None):
        parsed = urllib.parse.urlsplit(url)
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.query
                or '?' in url or '#' in url
                or parsed.scheme not in ('https', 'http')
                or (parsed.scheme == 'http' and (self.config.get('GLOBAL_ID_ALLOW_HTTP') != 'true' or not _private_host(parsed.hostname)))):
            raise ValueError('Global ID endpoint requires HTTPS')
        headers = {'Content-Type': content_type}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method='POST')
            with self.http.open(request, timeout=5) as response:
                body = response.read(100001)
                if response.status != 200 or len(body) > 100000:
                    raise ValueError('invalid response')
                return json.loads(body)
        except urllib.error.HTTPError as error:
            error.close()
            raise RuntimeError('Global ID authority unavailable or invalid response') from None
        except Exception:
            raise RuntimeError('Global ID authority unavailable or invalid response') from None

    def next(self, count=1):
        if type(count) is not int or not 1 <= count <= 1000:
            raise ValueError('count must be 1..1000')
        token = self._token()
        result = self._post(self.config['GLOBAL_ID_URL'].rstrip('/') + '/api/v1/ids',
                            json.dumps({'count': count}).encode(), 'application/json', token)
        return self._validate(result, count)

    @staticmethod
    def _validate(result, count):
        ids = result.get('ids') if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get('algorithm') not in ('snowflake-v1', 'snowflake-v2')
                or not isinstance(ids, list) or len(ids) != count
                or any(not isinstance(i, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', i)
                       or int(i) > 9223372036854775807 for i in ids)
                or len(set(ids)) != count):
            raise RuntimeError('Invalid Global ID response')
        return ids

    def _token(self):
        token = self.config.get('GLOBAL_ID_TOKEN')
        if not token and self._cached_token is not None and time.monotonic() < self._expires:
            return self._cached_token
        if not token:
            form = urllib.parse.urlencode({
                'grant_type': 'client_credentials', 'scope': 'global-id.generate',
                'client_id': self.config['GLOBAL_ID_CLIENT_ID'],
                'client_secret': self.config['GLOBAL_ID_CLIENT_SECRET'],
            }).encode()
            auth = self._post(self.config['GLOBAL_ID_TOKEN_URL'], form, 'application/x-www-form-urlencoded')
            if (not isinstance(auth, dict) or not isinstance(auth.get('access_token'), str)
                    or not auth['access_token'] or str(auth.get('token_type', '')).lower() != 'bearer'):
                raise RuntimeError('Invalid Global ID access token response')
            token = auth['access_token']
            lifetime = auth.get('expires_in')
            if type(lifetime) is int and 1 < lifetime <= 31536000:
                self._cached_token = token
                self._expires = time.monotonic() + lifetime - 1
        return token

    def close(self):
        with self._lock:
            if self._socket is not None:
                self._socket.close()
            self._socket = None
            self._socket_token = None

    def _exchange(self, message):
        deadline = time.monotonic() + 5
        self._socket.settimeout(5)
        self._socket.sendall(json.dumps(message).encode() + b'\n')
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            self._socket.settimeout(remaining)
            chunk = self._socket.recv(4096)
            if not chunk or len(body) + len(chunk) > 65537:
                raise RuntimeError('Invalid Global ID stream response')
            body.extend(chunk)
            if b'\n' in chunk:
                if not body.endswith(b'\n') or body.count(b'\n') != 1:
                    raise RuntimeError('Invalid Global ID stream framing')
                return json.loads(body)

    def next_tcp(self, count=1):
        if type(count) is not int or not 1 <= count <= 1000:
            raise ValueError('count must be 1..1000')
        with self._lock:
            try:
                token = self._token()
                if self._socket is not None and token != self._socket_token:
                    self.close()
                if self._socket is None:
                    raw = self.config['GLOBAL_ID_TCP_URL']
                    endpoint = urllib.parse.urlsplit(raw)
                    if (not endpoint.hostname or endpoint.port is None or not 1 <= endpoint.port <= 65535
                            or endpoint.username is not None or endpoint.password is not None or endpoint.path
                            or '?' in raw or '#' in raw or endpoint.scheme not in ('tcp', 'tls')
                            or (endpoint.scheme == 'tcp' and (self.config.get('GLOBAL_ID_ALLOW_HTTP') != 'true'
                                                             or not _private_host(endpoint.hostname)))):
                        raise ValueError('Invalid Global ID TCP endpoint')
                    self._socket = socket.create_connection((endpoint.hostname, endpoint.port), timeout=5)
                    if endpoint.scheme == 'tls':
                        self._socket = ssl.create_default_context().wrap_socket(self._socket, server_hostname=endpoint.hostname)
                    self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    ack = self._exchange({'type': 'auth', 'token': token})
                    if not isinstance(ack, dict) or ack.get('type') != 'authenticated':
                        raise RuntimeError('Global ID authentication failed')
                    self._socket_token = token
                self._sequence += 1
                request_id = str(self._sequence)
                result = self._exchange({'type': 'allocate', 'request_id': request_id, 'count': count})
                if not isinstance(result, dict) or result.get('type') != 'ids' or result.get('request_id') != request_id:
                    raise RuntimeError('Invalid Global ID correlated response')
                return self._validate(result, count)
            except Exception:
                self.close()
                raise RuntimeError('Global ID TCP authority unavailable or invalid response') from None
