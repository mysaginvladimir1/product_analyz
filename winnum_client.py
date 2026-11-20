"""Read-only WINNUM Platform API (Guide 5.5.3, pages 15, 25, 32, 40).

Never log requests exceptions: Winnum credentials are query parameters.
"""
import os
import time
import logging
from dataclasses import dataclass, field
from threading import RLock
from urllib.parse import unquote, urlparse

import requests
from lxml import etree

logger = logging.getLogger(__name__)


class WinnumError(Exception):
    """A sanitized error safe to show to a user."""


@dataclass(frozen=True)
class WinnumConfig:
    url: str
    appid: str
    user: str = field(repr=False)
    password: str = field(repr=False)
    timeout: float = 30
    catalog_ttl: int = 300

    @classmethod
    def from_env(cls):
        url = os.getenv('WINNUM_URL', '').strip()
        appid = os.getenv('WINNUM_APP_ID', '').strip()
        user = os.getenv('WINNUM_USER', '')
        password = os.getenv('WINNUM_PASSWORD', '')
        if not all((url, appid, user, password)):
            raise WinnumError('Заполните WINNUM_URL, WINNUM_APP_ID, WINNUM_USER и WINNUM_PASSWORD в .env.')
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or any(
                (parsed.username, parsed.password, parsed.query, parsed.fragment)):
            raise WinnumError('WINNUM_URL должен содержать адрес agw.jsp без параметров и учётных данных.')
        try:
            timeout = float(os.getenv('WINNUM_TIMEOUT', '30'))
            if not 0 < timeout <= 120:
                raise ValueError()
        except ValueError:
            raise WinnumError('WINNUM_TIMEOUT должен быть от 0 до 120 секунд.') from None
        return cls(url, appid, user, password, timeout)


class WinnumClient:
    def __init__(self, config):
        self.config = config
        self._catalog = None
        self._expires = 0
        self._lock = RLock()

    def clear_cache(self):
        with self._lock:
            self._catalog = None
            self._expires = 0

    def _get(self, helper, method, **params):
        started = time.monotonic()
        logger.info('Winnum request method=%s', method)
        params.update(rpc='winnum.views.url.' + helper, men=method, mode='yes',
                      usr=self.config.user, pwd=self.config.password)
        try:
            # No redirects: do not forward URL credentials to another destination.
            with requests.get(self.config.url, params=params, timeout=self.config.timeout,
                              allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise WinnumError(f'Winnum: HTTP {response.status_code} ({method}).')
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 16 * 1024 * 1024:
                        raise WinnumError('Слишком большой ответ Winnum. Сократите период или список станков.')
            parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
            root = etree.fromstring(bytes(body), parser)
            if root.tag != 'items' or root.get('status') != 'success':
                raise WinnumError(f'Winnum отклонил запрос {method}. Проверьте права и параметры.')
            rows = [{k: unquote(v) for k, v in node.attrib.items()} for node in root.findall('item')]
            logger.info('Winnum response method=%s items=%d elapsed=%.2fs', method, len(rows), time.monotonic() - started)
            return rows
        except requests.RequestException:
            raise WinnumError(f'Нет соединения с Winnum или истёк таймаут ({method}).') from None
        except etree.XMLSyntaxError:
            raise WinnumError(f'Winnum вернул некорректный XML ({method}).') from None

    def products(self):
        with self._lock:
            if self._catalog is None or time.monotonic() >= self._expires:
                rows = self._get('WNApplicationHelper', 'getProduct', oid=self.config.appid)
                if any(not r.get('id') or not r.get('name') or not r.get('ProductUUID') for r in rows):
                    raise WinnumError('В справочнике Winnum отсутствуют ID, UUID или названия станков.')
                self._catalog = tuple(dict(r) for r in rows)
                self._expires = time.monotonic() + self.config.catalog_ttl
            return [dict(r) for r in self._catalog]

    def operators(self, product, start, end):
        return self._get('WNCNCApplicationOperatorHelper', 'getOperatorsLoading',
                         appid=self.config.appid, pid=product['id'],
                         **{'from': start.isoformat(), 'till': end.isoformat()})

    def states(self, product, start, end):
        return self._get('WNApplicationTagHelper', 'getPriorityTagDuration',
                         appid=self.config.appid, pid=product['id'],
                         **{'from': start.isoformat(), 'till': end.isoformat()})

    def signals(self, product, signal, start, end):
        return self._get('WNConnectorHelper', 'getSignal', uuid=product['ProductUUID'],
                         signal=signal, stype='bytime', order='asc',
                         start=start.strftime('%Y-%m-%d %H:%M:%S'),
                         end=end.strftime('%Y-%m-%d %H:%M:%S'))
