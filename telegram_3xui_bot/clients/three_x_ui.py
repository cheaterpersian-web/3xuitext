import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple
import httpx

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

    async def list_inbounds(self) -> List[Dict[str, Any]]:
        resp = await self._request('GET', '/inbounds/list')
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to list inbounds: {resp.status_code} {resp.text[:200]}')
        # Ensure response is JSON; if HTML or empty, hint misconfiguration (e.g., wrong base path)
        try:
            data = resp.json()
        except Exception:
            snippet = (resp.text or '')[:200]
            raise ThreeXUIError(
                'Unexpected non-JSON from /inbounds/list. Check PANEL_BASE_URL (scheme/path) and credentials. '
                f'Response snippet: {snippet}'
            )
        # Some panels return {"obj": [ ... ]} or plain list
        if isinstance(data, dict) and 'obj' in data:
            return data['obj']
        if isinstance(data, list):
            return data
        return data.get('data') or []

    async def add_client(self, *, inbound_id: int, username: str, total_gb: float, expiry_days: int) -> Dict[str, Any]:
        # Map to bytes and expiry timestamp if needed
        total_bytes = int(total_gb * 1024 * 1024 * 1024)
        payload = {
            'inboundId': inbound_id,
            'email': username,
            'totalGB': total_bytes,
            'expiryTime': expiry_days,
            'enable': True,
        }
        resp = await self._request('POST', '/client/add', json=payload)
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to add client: {resp.status_code} {resp.text}')
        try:
            return resp.json()
        except Exception:
            return {'raw': resp.text}

    async def get_client_traffics(self, *, email: Optional[str] = None, client_id: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if email:
            params['email'] = email
        if client_id:
            params['id'] = client_id
        resp = await self._request('GET', '/client/traffics', params=params)
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to get traffics: {resp.status_code} {resp.text}')
        return resp.json()

    async def get_client_options(self, *, email: Optional[str] = None, client_id: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if email:
            params['email'] = email
        if client_id:
            params['id'] = client_id
        resp = await self._request('GET', '/client/options', params=params)
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to get client options: {resp.status_code} {resp.text}')
        return resp.json()

    async def get_online_clients(self) -> Dict[str, Any]:
        resp = await self._request('GET', '/online-clients')
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to get online clients: {resp.status_code} {resp.text}')
        return resp.json()

    async def reset_client_traffic(self, *, email: Optional[str] = None, client_id: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if email:
            payload['email'] = email
        if client_id:
            payload['id'] = client_id
        resp = await self._request('POST', '/client/reset', json=payload)
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to reset traffic: {resp.status_code} {resp.text}')
        return resp.json()

    async def delete_depleted_clients(self, *, inbound_id: Optional[int] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if inbound_id is not None:
            payload['inboundId'] = inbound_id
        resp = await self._request('POST', '/inbound/delete-depleted-clients', json=payload)
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to delete depleted clients: {resp.status_code} {resp.text}')
        return resp.json()

    async def delete_client(self, *, inbound_id: int, client_id: str) -> Dict[str, Any]:
        payload = {'inboundId': inbound_id, 'id': client_id}
        resp = await self._request('POST', '/client/delete', json=payload)
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to delete client: {resp.status_code} {resp.text}')
        return resp.json()

    async def get_inbound(self, *, inbound_id: int) -> Dict[str, Any]:
        resp = await self._request('GET', f'/inbound/{inbound_id}')
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to get inbound: {resp.status_code} {resp.text}')
        return resp.json()

    async def reset_inbounds_traffic(self) -> Dict[str, Any]:
        resp = await self._request('POST', '/reset-inbounds-traffic')
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to reset inbounds traffic: {resp.status_code} {resp.text}')
        return resp.json()

    async def reset_all_clients_traffic(self) -> Dict[str, Any]:
        resp = await self._request('POST', '/reset-all-clients-traffic')
        if resp.status_code >= 400:
            raise ThreeXUIError(f'Failed to reset all clients traffic: {resp.status_code} {resp.text}')
        return resp.json()

