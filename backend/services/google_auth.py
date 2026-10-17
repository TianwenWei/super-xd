"""Lazy ADC authentication inside the model request's existing async deadline."""
import asyncio
from collections import deque

import httpx

from .contracts import ProviderError


class GoogleADCAuth(httpx.Auth):
    def __init__(self, project):
        self.project = project
        self._credentials = None
        self._refresh_task = None
        self._lock = asyncio.Lock()
        self._tokens = deque(maxlen=8)

    @property
    def secrets(self):
        # Keep preceding tokens while requests using them may still be in flight.
        return tuple(self._tokens)

    def _load_and_refresh(self, credentials):
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
        from google.auth.transport.requests import Request
        import requests

        class BoundedRequest(Request):
            def __call__(self, *args, timeout=10, **kwargs):
                return super().__call__(*args, timeout=min(timeout or 10, 10), **kwargs)

        try:
            with requests.Session() as session:
                request = BoundedRequest(session=session)
                if credentials is None:
                    credentials, _ = google.auth.default(
                        scopes=['https://www.googleapis.com/auth/cloud-platform'],
                        request=request, quota_project_id=self.project)
                if not credentials.valid:
                    credentials.refresh(request)
                if not isinstance(credentials.token, str) or not credentials.token:
                    raise ValueError('ADC did not return an access token')
                return credentials, credentials.token
        except Exception as error:
            code = 'not_configured' if isinstance(error, DefaultCredentialsError) else 'auth'
            # Credential SDK exceptions can contain refresh tokens or metadata.
            # Preserve the failure category without exporting their raw arguments.
            raise ProviderError(code, 'Google ADC discovery or refresh failed',
                provider='google_vertex', details={'error_type': type(error).__name__}) from None

    async def _token(self):
        async with self._lock:
            if self._refresh_task is None:
                if self._credentials is not None and self._credentials.valid:
                    return self._credentials.token
                self._refresh_task = asyncio.create_task(
                    asyncio.to_thread(self._load_and_refresh, self._credentials))
                # Every waiter may time out; still retrieve a later refresh error.
                self._refresh_task.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception())
            task = self._refresh_task
        try:
            # A cancelled request must not cancel a shared refresh or unlock a
            # second refresh while the original synchronous SDK call is running.
            credentials, token = await asyncio.shield(task)
        except Exception:
            async with self._lock:
                if self._refresh_task is task:
                    self._refresh_task = None
            raise
        async with self._lock:
            if self._refresh_task is task:
                self._credentials = credentials
                self._refresh_task = None
            if token not in self._tokens:
                self._tokens.append(token)
        return token

    async def request_headers(self):
        """Share one refresh with HTTP or gRPC callers; callers own their deadline."""
        return {'Authorization': 'Bearer ' + await self._token(),
                'x-goog-user-project': self.project}

    async def async_auth_flow(self, request):
        request.headers.update(await self.request_headers())
        yield request
