"""
OpenShift package — Red Hat Assisted Installer API client and SSO token helper.
"""

import hashlib
import time

import requests as _req

from .constants import AI_BASE, SSO_URL

# FEATURE: token-cache
_token_cache: dict = {}   # ps_hash → { token, expires_at }


# FEATURE: sso-auth

def _get_access_token(offline_token: str) -> str:
    """Exchange a Red Hat offline token for a short-lived access token via RH SSO.

    NOTE: This is NOT the same as the pull-secret registry credential.
    The offline token must be obtained separately from:
      https://console.redhat.com/openshift/token
    """
    tok_hash = hashlib.sha256(offline_token.encode()).hexdigest()[:16]
    now = time.time()
    cached = _token_cache.get(tok_hash)
    if cached and cached['expires_at'] > now + 60:
        return cached['token']

    resp = _req.post(
        SSO_URL,
        data={
            'grant_type':    'refresh_token',
            'client_id':     'cloud-services',
            'refresh_token': offline_token.strip(),
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=20,
    )
    if not resp.ok:
        body = resp.text[:400]
        raise RuntimeError(
            f'SSO returned HTTP {resp.status_code}: {body}'
        )
    data = resp.json()
    token = data['access_token']
    _token_cache[tok_hash] = {'token': token, 'expires_at': now + data.get('expires_in', 900)}
    return token


# FEATURE: ai-api-client

def _ai(method: str, path: str, token: str, body=None, stream=False, timeout=30):
    """Make a request to the Assisted Installer API."""
    url = f'{AI_BASE}{path}'
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type':  'application/json',
    }
    resp = _req.request(method, url, headers=headers,
                        json=body, stream=stream, timeout=timeout)
    if not resp.ok:
        try:
            detail = resp.json()
            msg = detail.get('message') or detail.get('reason') or str(detail)
        except Exception:
            msg = resp.text[:500]
        raise Exception(f'{resp.status_code} {resp.reason}: {msg}')
    return resp
