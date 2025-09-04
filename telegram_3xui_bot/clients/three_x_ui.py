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
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            verify=not insecure,
            follow_redirects=True,
            headers={'Accept': 'application/json, text/plain, */*'}
        )
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

    async def _ensure_api_prefix(self) -> None:
        if self._api_prefix is None:
            try:
                await self._probe_inbounds_endpoint()
            except ThreeXUIError:
                # Leave prefix unset; direct paths may still work
                pass

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
        # Try documented path first
        resp = await self._request('GET', self._build_url('/panel/api/inbounds/list'))
        if resp.status_code >= 400:
            # Fallback to probing across flavors
            return await self._probe_inbounds_endpoint()
        try:
            data = resp.json()
        except Exception:
            # If 200 but non-JSON, try probing to detect correct prefix/variant
            try:
                return await self._probe_inbounds_endpoint()
            except ThreeXUIError:
                snippet = (resp.text or '')[:200]
                raise ThreeXUIError(
                    f'Unexpected non-JSON from /panel/api/inbounds/list. Check PANEL_BASE_URL (scheme/path) and credentials. '
                    f'Response snippet: {snippet}'
                )
        # Some panels return {"obj": [ ... ]} or plain list
        if isinstance(data, dict) and 'obj' in data:
            return data['obj']
        if isinstance(data, list):
            return data
        return data.get('data') or []

    async def add_client(self, *, inbound_id: int, username: str, total_gb: float, expiry_days: int, client_uuid: Optional[str] = None, sub_id: Optional[str] = None) -> Dict[str, Any]:
        # Use detected API prefix and accept 3X-UI variants; send form-encoded body
        total_bytes = int(total_gb * 1024 * 1024 * 1024)
        # per your example: allow zero for unlimited
        expiry_ts_ms = 0 if int(expiry_days) == 0 else int((_time.time() + expiry_days * 86400) * 1000)
        payload_settings = __import__('json').dumps({
            'clients': [{
                'id': client_uuid or str(__import__('uuid').uuid4()),
                'flow': '',
                'email': username,
                'limitIp': 0,
                'totalGB': total_bytes,
                'expiryTime': expiry_ts_ms,
                'enable': True,
                'tgId': '',
                'subId': sub_id or ''.join(__import__('random').choices('abcdefghijklmnopqrstuvwxyz0123456789', k=16)),
                'reset': 0,
            }]
        })
        data = {
            'id': inbound_id,
            'inboundId': inbound_id,
            'settings': payload_settings,
        }
        # Ensure API prefix (e.g., '', '/panel', '/panel/api')
        await self._ensure_api_prefix()
        # Try preferred plural first, then singular route name variants
        url_candidates = [
            self._build_url('/inbounds/addClient'),
            self._build_url('/inbound/addClient'),
            self._build_url('/client/add'),
            # Some panels expose explicit panel/api path regardless of prefix detection
            '/panel/api/inbounds/addClient',
            '/panel/api/client/add',
            '/xui/inbounds/addClient',
            '/xui/inbound/addClient',
        ]
        last_resp: Optional[httpx.Response] = None
        last_err: Optional[str] = None
        async def _verify_created() -> bool:
            try:
                info = await self.get_client_traffics(email=username)
                if isinstance(info, dict) and (info or 'obj' in info):
                    return True
            except Exception:
                pass
            try:
                inbound = await self.get_inbound(inbound_id=inbound_id)
                inb = inbound.get('obj') if isinstance(inbound, dict) and 'obj' in inbound else inbound
                settings_raw = (inb or {}).get('settings') if isinstance(inb, dict) else None
                if isinstance(settings_raw, str) and settings_raw.strip():
                    try:
                        settings = json.loads(settings_raw)
                        for cli in settings.get('clients') or []:
                            try:
                                if (cli.get('email') or '').strip() == username:
                                    return True
                            except Exception:
                                continue
                    except Exception:
                        pass
            except Exception:
                pass
            return False

        for url in url_candidates:
            resp = await self._request('POST', url, data=data)
            last_resp = resp
            ctype = (resp.headers.get('content-type') or '').lower()
            if resp.status_code >= 400:
                last_err = f"{resp.status_code} {resp.text[:200]}"
                continue
            # Accept JSON
            try:
                parsed = resp.json()
                if isinstance(parsed, dict) and parsed.get('success') is False:
                    last_err = parsed.get('msg') or parsed.get('message') or 'success=false'
                    # do not return; try next variant
                    continue
                # verify actually created on panel
                if await _verify_created():
                    return parsed if isinstance(parsed, (dict, list)) else {'success': True, 'raw': resp.text}
            except Exception:
                pass
            # Accept text/plain or empty body with 200
            text = (resp.text or '').strip()
            if 200 <= resp.status_code < 300:
                if await _verify_created():
                    return {'success': True, 'raw': text, 'content_type': ctype}
            last_err = f"{resp.status_code} {ctype} {text[:200]}"
            # Try JSON body variant as some panels expect JSON
            resp = await self._request('POST', url, json=data)
            last_resp = resp
            ctype = (resp.headers.get('content-type') or '').lower()
            if resp.status_code >= 400:
                last_err = f"{resp.status_code} {resp.text[:200]}"
                continue
            try:
                parsed = resp.json()
                if isinstance(parsed, dict) and parsed.get('success') is False:
                    last_err = parsed.get('msg') or parsed.get('message') or 'success=false'
                else:
                    if await _verify_created():
                        return parsed if isinstance(parsed, (dict, list)) else {'success': True, 'raw': resp.text}
            except Exception:
                pass
            text = (resp.text or '').strip()
            if 200 <= resp.status_code < 300:
                if await _verify_created():
                    return {'success': True, 'raw': text, 'content_type': ctype}
            last_err = f"{resp.status_code} {ctype} {text[:200]}"
        # If we reached here, all candidates failed
        raise ThreeXUIError(f"addClient failed: {last_err or 'unknown error'}")

    async def get_client_traffics(self, *, email: Optional[str] = None, client_id: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if email:
            params['email'] = email
        if client_id:
            params['id'] = client_id
        # Prefer dynamic API prefix and both inbound(s) variants
        await self._ensure_api_prefix()
        last_resp: Optional[httpx.Response] = None
        if email:
            candidates = [
                self._build_url(f'/inbounds/getClientTraffics/{email}'),
                self._build_url(f'/inbound/getClientTraffics/{email}'),
                # Some flavors expose client traffic under client endpoint
                self._build_url(f'/client/traffics?email={email}'),
                '/panel/api/inbounds/getClientTraffics/' + email,
            ]
            for url in candidates:
                resp = await self._request('GET', url)
                last_resp = resp
                if resp.status_code >= 400:
                    continue
                try:
                    data = resp.json()
                    if isinstance(data, dict) and 'obj' in data:
                        return data['obj']
                    return data
                except Exception:
                    # Accept text/plain simple kv pairs like up/down/total if present
                    text = (resp.text or '').strip()
                    if text and any(k in text for k in ('up', 'down', 'total')):
                        # naive parse: extract integers after keys
                        try:
                            kv: Dict[str, Any] = {}
                            for part in text.replace(',', '\n').split('\n'):
                                if ':' in part:
                                    k, v = part.split(':', 1)
                                    k = k.strip()
                                    v = v.strip()
                                    if v.isdigit():
                                        kv[k] = int(v)
                                    else:
                                        try:
                                            kv[k] = int(float(v))
                                        except Exception:
                                            kv[k] = v
                            if kv:
                                return kv
                        except Exception:
                            pass
                    # try next variant
                    continue
        else:
            resp = await self._request('GET', self._build_url('/client/traffics'), params=params)
            last_resp = resp
            if resp.status_code < 400:
                try:
                    data = resp.json()
                    if isinstance(data, dict) and 'obj' in data:
                        return data['obj']
                    return data
                except Exception:
                    # Treat 200 non-JSON as opaque success payload
                    text = (resp.text or '').strip()
                    if text:
                        return {'raw': text}
                    snippet = (resp.text or '')[:200]
                    raise ThreeXUIError(f'Unexpected non-JSON from traffics endpoint. Snippet: {snippet}')
        if last_resp is None:
            raise ThreeXUIError('Failed to get client traffics: no response')
        snippet = (last_resp.text or '')[:200]
        raise ThreeXUIError(f'Failed to get client traffics: {last_resp.status_code} {snippet}')
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

