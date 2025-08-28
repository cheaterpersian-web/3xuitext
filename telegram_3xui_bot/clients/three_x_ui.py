import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple
import httpx
import json
import time as _time

class ThreeXUIError(Exception):
    pass

class AuthenticationError(ThreeXUIError):
    pass

class ThreeXUIClient:
    def __init__(self, base_url: str, username: str, password: str, *, insecure: bool = False, timeout: float = 20.0):
        self._base_url = base_url.rstrip('/')
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout, verify=not insecure, follow_redirects=True)
        self._last_login_at: float = 0.0
        self._api_prefix: str | None = None
        self._use_plural_inbounds: bool | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def login(self) -> None:
        payload = {"username": self._username, "password": self._password}
        # Some 3X-UI variants expect form-encoded, others accept JSON
        resp = await self._client.post('/login', data=payload)
        if resp.status_code >= 400:
            # fallback to JSON body once
            resp = await self._client.post('/login', json=payload)
            if resp.status_code >= 400:
                raise AuthenticationError(f'Login failed: {resp.status_code} {resp.text[:200]}')
        # Rely on Set-Cookie: session
        if 'session' not in resp.cookies and 'session' not in self._client.cookies:
            # Some panels set cookie via redirect; after follow_redirects, use client's cookie jar
            cookies = self._client.cookies.jar if hasattr(self._client.cookies, 'jar') else self._client.cookies
            if not cookies:
                raise AuthenticationError('Login did not yield a session cookie')
        self._last_login_at = time.time()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        # Ensure we have logged in at least once
        if self._last_login_at == 0.0:
            await self.login()
        resp = await self._client.request(method, url, **kwargs)
        if resp.status_code == 401:
            # try re-login once
            await self.login()
            resp = await self._client.request(method, url, **kwargs)
        return resp

    def _build_url(self, path: str) -> str:
        prefix = self._api_prefix or ''
        return f"{prefix}{path}"

    async def _probe_inbounds_endpoint(self) -> list[dict[str, any]]:
        prefixes = ['', '/xui', '/panel', '/api', '/xui/api', '/panel/api']
        tried: list[str] = []
        for prefix in prefixes:
            for use_plural in (True, False):
                path = '/inbounds/list' if use_plural else '/inbound/list'
                full = f"{prefix}{path}"
                tried.append(full)
                resp = await self._request('GET', full)
                if resp.status_code >= 400:
                    continue
                try:
                    data = resp.json()
                except Exception:
                    continue
                # Normalize
                if isinstance(data, dict) and 'obj' in data and isinstance(data['obj'], list):
                    self._api_prefix = prefix
                    self._use_plural_inbounds = use_plural
                    return data['obj']
                if isinstance(data, list):
                    self._api_prefix = prefix
                    self._use_plural_inbounds = use_plural
                    return data
                inner = data.get('data') if isinstance(data, dict) else None
                if isinstance(inner, list):
                    self._api_prefix = prefix
                    self._use_plural_inbounds = use_plural
                    return inner
        raise ThreeXUIError(
            'Could not locate inbounds endpoint. Tried: ' + ', '.join(tried)
        )

    async def list_inbounds(self) -> List[Dict[str, Any]]:
        # Probe once to find correct prefix and pluralization if unknown
        if self._api_prefix is None or self._use_plural_inbounds is None:
            return await self._probe_inbounds_endpoint()

        path = '/inbounds/list' if self._use_plural_inbounds else '/inbound/list'
        url = f"{self._api_prefix}{path}"
        resp = await self._request('GET', url)
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to list inbounds: {resp.status_code} {resp.text[:200]}')
        try:
            data = resp.json()
        except Exception:
            snippet = (resp.text or '')[:200]
            raise ThreeXUIError(
                f'Unexpected non-JSON from {url}. Check PANEL_BASE_URL (scheme/path) and credentials. '
                f'Response snippet: {snippet}'
            )
        # Some panels return {"obj": [ ... ]} or plain list
        if isinstance(data, dict) and 'obj' in data:
            return data['obj']
        if isinstance(data, list):
            return data
        return data.get('data') or []

    async def add_client(self, *, inbound_id: int, username: str, total_gb: float, expiry_days: int, client_uuid: Optional[str] = None, sub_id: Optional[str] = None) -> Dict[str, Any]:
        # Try multiple endpoint/payload variants across panel flavors
        total_bytes = int(total_gb * 1024 * 1024 * 1024)
        expiry_ts_ms = int((_time.time() + expiry_days * 86400) * 1000)
        # Variant A: inbounds/addClient with settings JSON (most common for panel/api)
        settings_payload = {
            'id': inbound_id,
            'settings': httpx.dumps({
                'clients': [{
                    'id': client_uuid or str(__import__('uuid').uuid4()),
                    'alterId': 0,
                    'email': username,
                    'totalGB': total_bytes,
                    'expiryTime': -2592000000,
                    'enable': True,
                    'tgId': '',
                    'subId': sub_id or ''.join(__import__('random').choices('abcdefghijklmnopqrstuvwxyz0123456789', k=16)),
                    'limitIp': 0,
                    'flow': '',
                }]
            }) if hasattr(httpx, 'dumps') else __import__('json').dumps({
                'clients': [{
                    'id': client_uuid or str(__import__('uuid').uuid4()),
                    'alterId': 0,
                    'email': username,
                    'totalGB': total_bytes,
                    'expiryTime': -2592000000,
                    'enable': True,
                    'tgId': '',
                    'subId': sub_id or ''.join(__import__('random').choices('abcdefghijklmnopqrstuvwxyz0123456789', k=16)),
                    'limitIp': 0,
                    'flow': '',
                }]
            })
        }

        payloads = [
            settings_payload,
            {
                'inboundId': inbound_id,
                'email': username,
                'totalGB': int(round(total_gb)),
                'expiryTime': int(expiry_days),
                'enable': True,
                'limitIp': 0,
            },
            {
                'inboundId': inbound_id,
                'email': username,
                'total': total_bytes,
                'expireTime': expiry_ts_ms,
                'enable': True,
                'limitIp': 0,
            },
        ]
        # Prioritize the exact path used in your working script
        paths = ['/panel/api/inbounds/addClient', '/inbounds/addClient', '/inbound/addClient', '/client/add']
        errors: List[str] = []
        for path in paths:
            url = self._build_url(path)
            for pl in payloads:
                resp = await self._request('POST', url, json=pl)
                if resp.status_code >= 400:
                    errors.append(f"{url} -> {resp.status_code}")
                    continue
                try:
                    return resp.json()
                except Exception:
                    return {'raw': resp.text}
        raise ThreeXUIError('Failed to add client via known endpoints. Tried: ' + ', '.join(errors))

    async def get_client_traffics(self, *, email: Optional[str] = None, client_id: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if email:
            params['email'] = email
        if client_id:
            params['id'] = client_id
        resp = await self._request('GET', self._build_url('/client/traffics'), params=params)
        if resp.status_code >= 400 or not (resp.headers.get('content-type','').startswith('application/json')):
            # Fallback to inbounds/getClientTraffics/<email>
            if email:
                alt_url = self._build_url(f'/inbounds/getClientTraffics/{email}')
                resp = await self._request('GET', alt_url)
        try:
            data = resp.json()
        except Exception:
            snippet = (resp.text or '')[:200]
            raise ThreeXUIError(f'Unexpected non-JSON from traffics endpoint. Snippet: {snippet}')
        if isinstance(data, dict) and 'obj' in data:
            return data['obj']
        return data

    async def get_client_options(self, *, email: Optional[str] = None, client_id: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if email:
            params['email'] = email
        if client_id:
            params['id'] = client_id
        resp = await self._request('GET', self._build_url('/client/options'), params=params)
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to get client options: {resp.status_code} {resp.text}')
        return resp.json()

    async def get_online_clients(self) -> Dict[str, Any]:
        resp = await self._request('GET', self._build_url('/online-clients'))
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to get online clients: {resp.status_code} {resp.text}')
        return resp.json()

    async def reset_client_traffic(self, *, email: Optional[str] = None, client_id: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if email:
            payload['email'] = email
        if client_id:
            payload['id'] = client_id
        resp = await self._request('POST', self._build_url('/client/reset'), json=payload)
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to reset traffic: {resp.status_code} {resp.text}')
        return resp.json()

    async def delete_depleted_clients(self, *, inbound_id: Optional[int] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if inbound_id is not None:
            payload['inboundId'] = inbound_id
        resp = await self._request('POST', self._build_url('/inbound/delete-depleted-clients'), json=payload)
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to delete depleted clients: {resp.status_code} {resp.text}')
        return resp.json()

    async def delete_client(self, *, inbound_id: int, client_id: str) -> Dict[str, Any]:
        payload = {'inboundId': inbound_id, 'id': client_id}
        resp = await self._request('POST', self._build_url('/client/delete'), json=payload)
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to delete client: {resp.status_code} {resp.text}')
        return resp.json()

    async def get_inbound(self, *, inbound_id: int) -> Dict[str, Any]:
        # Try multiple forms
        candidates = [
            self._build_url(f'/inbound/{inbound_id}'),
            self._build_url(f'/inbounds/{inbound_id}'),
            self._build_url(f'/inbounds/get/{inbound_id}'),
        ]
        last_resp: Optional[httpx.Response] = None
        for url in candidates:
            resp = await self._request('GET', url)
            last_resp = resp
            if resp.status_code >= 400:
                continue
            try:
                return resp.json()
            except Exception:
                continue
        if last_resp is None:
            raise ThreeXUIError('Failed to get inbound: no response')
        raise ThreeXUIError(f'Failed to get inbound: {last_resp.status_code} {last_resp.text[:200]}')

    async def reset_inbounds_traffic(self) -> Dict[str, Any]:
        resp = await self._request('POST', self._build_url('/reset-inbounds-traffic'))
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to reset inbounds traffic: {resp.status_code} {resp.text}')
        return resp.json()

    async def reset_all_clients_traffic(self) -> Dict[str, Any]:
        resp = await self._request('POST', self._build_url('/reset-all-clients-traffic'))
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to reset all clients traffic: {resp.status_code} {resp.text}')
        return resp.json()

