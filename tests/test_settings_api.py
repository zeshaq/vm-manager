"""
Tests for the system-wide Settings API (issue #15).

Verifies:
  1. GET /api/settings  returns {has, masked} — never plaintext
  2. GET /api/settings/reveal  returns plaintext values (auth-gated)
  3. POST /api/settings  saves / updates / clears keys
  4. DELETE /api/settings/<key>  removes a single key
  5. Auth guard on every endpoint
  6. get_secret() helper returns stored values
  7. File written atomically with 0o600 permissions
"""

import json
import os
import stat
import tempfile
import pytest
from unittest.mock import patch

import views.settings as settings_mod
from views.settings import get_secret, SETTINGS_KEYS


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def tmp_secrets(tmp_path):
    """Redirect secrets file to a temporary location for each test."""
    fake_file = str(tmp_path / 'secrets.json')
    with patch.object(settings_mod, '_SECRETS_FILE', fake_file):
        yield fake_file


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/settings — masked list
# ─────────────────────────────────────────────────────────────────────────────

class TestGetSettings:

    def test_returns_has_and_masked_keys(self, client, tmp_secrets):
        r = client.get('/api/settings')
        assert r.status_code == 200
        data = r.get_json()
        assert 'has'    in data
        assert 'masked' in data

    def test_has_contains_all_setting_keys(self, client, tmp_secrets):
        data = client.get('/api/settings').get_json()
        for key in SETTINGS_KEYS:
            assert key in data['has'], f"Missing key in 'has': {key}"

    def test_has_false_when_no_secrets_file(self, client, tmp_secrets):
        data = client.get('/api/settings').get_json()
        for key in SETTINGS_KEYS:
            assert data['has'][key] is False

    def test_has_true_after_save(self, client, tmp_secrets):
        client.post('/api/settings', json={'pull_secret': 'mysecret'})
        data = client.get('/api/settings').get_json()
        assert data['has']['pull_secret'] is True

    def test_masked_never_exposes_plaintext(self, client, tmp_secrets):
        secret = 'supersecretpullsecret1234567890'
        client.post('/api/settings', json={'pull_secret': secret})
        data = client.get('/api/settings').get_json()
        assert secret not in data['masked']['pull_secret']
        assert '•' in data['masked']['pull_secret']

    def test_masked_shows_prefix_and_suffix(self, client, tmp_secrets):
        secret = 'ABCD1234567890WXYZ'
        client.post('/api/settings', json={'pull_secret': secret})
        masked = client.get('/api/settings').get_json()['masked']['pull_secret']
        assert masked.startswith('ABCD')
        assert masked.endswith('WXYZ')

    def test_requires_auth(self, anon_client, tmp_secrets):
        assert anon_client.get('/api/settings').status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/settings/reveal — plaintext
# ─────────────────────────────────────────────────────────────────────────────

class TestRevealSettings:

    def test_returns_all_keys(self, client, tmp_secrets):
        data = client.get('/api/settings/reveal').get_json()
        for key in SETTINGS_KEYS:
            assert key in data

    def test_returns_empty_string_for_unset_keys(self, client, tmp_secrets):
        data = client.get('/api/settings/reveal').get_json()
        for key in SETTINGS_KEYS:
            assert data[key] == ''

    def test_returns_plaintext_after_save(self, client, tmp_secrets):
        client.post('/api/settings', json={
            'pull_secret':    'my-pull-secret',
            'ssh_public_key': 'ssh-rsa AAAA...',
        })
        data = client.get('/api/settings/reveal').get_json()
        assert data['pull_secret']    == 'my-pull-secret'
        assert data['ssh_public_key'] == 'ssh-rsa AAAA...'

    def test_requires_auth(self, anon_client, tmp_secrets):
        assert anon_client.get('/api/settings/reveal').status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/settings — save / update / clear
# ─────────────────────────────────────────────────────────────────────────────

class TestSaveSettings:

    def test_save_single_key(self, client, tmp_secrets):
        r = client.post('/api/settings', json={'pull_secret': 'tok123'})
        assert r.status_code == 200
        assert r.get_json()['ok'] is True

    def test_save_multiple_keys_at_once(self, client, tmp_secrets):
        client.post('/api/settings', json={
            'pull_secret':    'ps',
            'ssh_public_key': 'key',
            'rh_offline_token': 'rh',
            'cloudflare_token': 'cf',
        })
        revealed = client.get('/api/settings/reveal').get_json()
        assert revealed['pull_secret']      == 'ps'
        assert revealed['ssh_public_key']   == 'key'
        assert revealed['rh_offline_token'] == 'rh'
        assert revealed['cloudflare_token'] == 'cf'

    def test_empty_string_clears_key(self, client, tmp_secrets):
        client.post('/api/settings', json={'pull_secret': 'tok'})
        client.post('/api/settings', json={'pull_secret': ''})
        data = client.get('/api/settings').get_json()
        assert data['has']['pull_secret'] is False

    def test_update_preserves_other_keys(self, client, tmp_secrets):
        client.post('/api/settings', json={'pull_secret': 'ps', 'rh_offline_token': 'rh'})
        client.post('/api/settings', json={'pull_secret': 'ps-updated'})
        revealed = client.get('/api/settings/reveal').get_json()
        assert revealed['rh_offline_token'] == 'rh'     # untouched
        assert revealed['pull_secret']      == 'ps-updated'

    def test_whitespace_only_clears_key(self, client, tmp_secrets):
        client.post('/api/settings', json={'pull_secret': 'tok'})
        client.post('/api/settings', json={'pull_secret': '   '})
        assert client.get('/api/settings').get_json()['has']['pull_secret'] is False

    def test_updated_list_in_response(self, client, tmp_secrets):
        r = client.post('/api/settings', json={'pull_secret': 'x', 'ssh_public_key': 'y'})
        updated = r.get_json()['updated']
        assert 'pull_secret'    in updated
        assert 'ssh_public_key' in updated

    def test_requires_auth(self, anon_client, tmp_secrets):
        assert anon_client.post('/api/settings', json={'pull_secret': 'x'}).status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /api/settings/<key> — clear a single key
# ─────────────────────────────────────────────────────────────────────────────

class TestClearSetting:

    def test_clear_existing_key(self, client, tmp_secrets):
        client.post('/api/settings', json={'pull_secret': 'tok'})
        r = client.delete('/api/settings/pull_secret')
        assert r.status_code == 200
        assert r.get_json()['ok'] is True
        assert client.get('/api/settings').get_json()['has']['pull_secret'] is False

    def test_clear_nonexistent_key_still_200(self, client, tmp_secrets):
        r = client.delete('/api/settings/pull_secret')
        assert r.status_code == 200

    def test_clear_unknown_key_returns_400(self, client, tmp_secrets):
        r = client.delete('/api/settings/unknown_key_xyz')
        assert r.status_code == 400

    def test_clear_preserves_other_keys(self, client, tmp_secrets):
        client.post('/api/settings', json={'pull_secret': 'ps', 'rh_offline_token': 'rh'})
        client.delete('/api/settings/pull_secret')
        revealed = client.get('/api/settings/reveal').get_json()
        assert revealed['pull_secret']      == ''
        assert revealed['rh_offline_token'] == 'rh'

    def test_requires_auth(self, anon_client, tmp_secrets):
        assert anon_client.delete('/api/settings/pull_secret').status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# get_secret() helper
# ─────────────────────────────────────────────────────────────────────────────

class TestGetSecretHelper:

    def test_returns_stored_value(self, client, tmp_secrets):
        client.post('/api/settings', json={'pull_secret': 'direct-value'})
        assert get_secret('pull_secret') == 'direct-value'

    def test_returns_default_for_missing_key(self, tmp_secrets):
        assert get_secret('pull_secret') == ''
        assert get_secret('pull_secret', 'fallback') == 'fallback'

    def test_returns_empty_when_no_file(self, tmp_secrets):
        assert get_secret('rh_offline_token') == ''


# ─────────────────────────────────────────────────────────────────────────────
# File persistence and security
# ─────────────────────────────────────────────────────────────────────────────

class TestFilePersistence:

    def test_file_created_on_save(self, client, tmp_secrets):
        assert not os.path.exists(tmp_secrets)
        client.post('/api/settings', json={'pull_secret': 'x'})
        assert os.path.exists(tmp_secrets)

    def test_file_is_valid_json(self, client, tmp_secrets):
        client.post('/api/settings', json={'pull_secret': 'hello'})
        with open(tmp_secrets) as f:
            data = json.load(f)
        assert data['pull_secret'] == 'hello'

    def test_file_permissions_restricted(self, client, tmp_secrets):
        client.post('/api/settings', json={'pull_secret': 'x'})
        mode = oct(stat.S_IMODE(os.stat(tmp_secrets).st_mode))
        assert mode == '0o600', f"Expected 0o600, got {mode}"

    def test_survives_empty_json_file(self, tmp_secrets):
        with open(tmp_secrets, 'w') as f:
            f.write('')
        # Should not crash — returns empty dict
        assert get_secret('pull_secret') == ''

    def test_survives_corrupt_json_file(self, tmp_secrets):
        with open(tmp_secrets, 'w') as f:
            f.write('{corrupt json:::}')
        assert get_secret('pull_secret') == ''
