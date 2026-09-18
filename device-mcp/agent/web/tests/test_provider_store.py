import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provider_store import ProviderStore


class FakeCredentialBackend:
    def __init__(self):
        self.secret = None

    def read(self):
        return self.secret

    def write(self, secret):
        self.secret = secret

    def delete(self):
        self.secret = None


def test_remembered_provider_keeps_key_out_of_settings_file(tmp_path):
    backend = FakeCredentialBackend()
    store = ProviderStore(tmp_path / "provider.json", backend=backend)

    store.save("kimi-super-secret", "https://gateway.example.com/coding/", "k3-256k")

    raw = (tmp_path / "provider.json").read_text(encoding="utf-8")
    assert "kimi-super-secret" not in raw
    assert json.loads(raw) == {
        "base_url": "https://gateway.example.com/coding/",
        "model": "k3-256k",
    }
    assert backend.secret == "kimi-super-secret"
    assert store.load() == {
        "api_key": "kimi-super-secret",
        "base_url": "https://gateway.example.com/coding/",
        "model": "k3-256k",
    }


def test_forget_provider_removes_secret_and_metadata(tmp_path):
    backend = FakeCredentialBackend()
    store = ProviderStore(tmp_path / "provider.json", backend=backend)
    store.save("kimi-super-secret", "https://gateway.example.com/", "k3")

    store.delete()

    assert backend.secret is None
    assert not (tmp_path / "provider.json").exists()
    assert store.load() is None
