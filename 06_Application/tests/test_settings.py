"""User settings on disk: the user's OWN Anthropic key, encrypted at rest.

The project is going open source. Until now the only way to give evalkit a key
was to edit `06_Application/.env`, which is fine for the author and impossible
for anybody else. So the key becomes a SETTING -- pasted in the app, kept in the
service state directory, and never written in the clear.

What is pinned here, and why each one is a bug that has actually shipped
somewhere:

* ENCRYPTED AT REST. The assertion is not "we called encrypt", it is that the
  bytes on disk do not contain the key. A sentinel makes that checkable;
* ATOMIC. A half-written settings file must not lose a key the user already
  pasted, so the write is `.tmp` + `os.replace` and leaves no debris;
* PERMISSIONS. `os.chmod(0o600)` is a SILENT NO-OP on Windows. The POSIX
  assertion is the mode; the Windows one cannot be (see the test), so what is
  pinned there is that the ACL call does not break the store;
* VALIDATION IN SPANISH, at paste time. A user who pastes the wrong string must
  learn immediately, not when a paid stage fails an hour later.
"""
import json
import os

import pytest

from lxp_eval.service.crypto import SecretBox, ServiceError, generate_key
from lxp_eval.service.settings import API_KEY_PREFIX, SettingsStore

#: A realistic-shaped key carrying a sentinel, so "is the plaintext on disk?"
#: is a question about bytes rather than about our own good intentions.
KEY = "sk-ant-api03-SENTINELA-de-prueba-que-no-debe-quedar-en-claro-AA"


@pytest.fixture
def box(monkeypatch):
    # The SAME SecretBox the service already uses for platform credentials --
    # one key, one crypto path, per `crypto.py`.
    monkeypatch.setenv("EVALKIT_SECRET_KEY", generate_key())
    return SecretBox.from_env()


@pytest.fixture
def store(tmp_path, box):
    # The same root the JobStore uses: create_app hands both the same directory.
    return SettingsStore(tmp_path / "runs" / "jobs", box)


# --- round trip --------------------------------------------------------------


def test_the_key_round_trips(store):
    assert store.get_api_key() is None
    assert store.has_api_key() is False
    store.set_api_key(KEY)
    assert store.get_api_key() == KEY
    assert store.has_api_key() is True


def test_the_key_survives_a_restart(tmp_path, box):
    """A fresh store over the same root -- i.e. the container came back."""
    SettingsStore(tmp_path / "runs" / "jobs", box).set_api_key(KEY)
    assert SettingsStore(tmp_path / "runs" / "jobs", box).get_api_key() == KEY


# --- encrypted at rest -------------------------------------------------------


def test_the_file_on_disk_never_holds_the_key_in_plaintext(store):
    store.set_api_key(KEY)
    raw = store.path.read_bytes()
    assert b"SENTINELA" not in raw
    assert KEY.encode("utf-8") not in raw
    # ...and it is still a readable settings file, not an opaque blob: the
    # ciphertext is a FIELD, so a future setting can be added without a
    # migration.
    assert isinstance(json.loads(raw.decode("utf-8")), dict)


def test_no_directory_under_the_root_holds_the_key_in_plaintext(store, tmp_path):
    store.set_api_key(KEY)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"SENTINELA" not in path.read_bytes(), path


def test_the_repr_does_not_carry_the_key(store):
    store.set_api_key(KEY)
    assert "SENTINELA" not in repr(store)
    assert "SENTINELA" not in str(store)


# --- atomic write ------------------------------------------------------------


def test_the_write_is_atomic_and_leaves_no_debris(store):
    store.set_api_key(KEY)
    store.set_api_key(KEY.replace("AA", "BB"))
    assert store.get_api_key() == KEY.replace("AA", "BB")
    assert [p.name for p in store.root.iterdir()] == [store.path.name]


def test_a_corrupt_settings_file_is_not_a_crash(store):
    """Truncated JSON reads as "no key set", not as a 500 on the settings screen."""
    store.set_api_key(KEY)
    store.path.write_text('{"anthropicApiKey": "no-es-un-token-f', encoding="utf-8")
    assert store.get_api_key() is None
    assert store.has_api_key() is False
    store.set_api_key(KEY)          # ...and the user can simply re-paste
    assert store.get_api_key() == KEY


# --- validation --------------------------------------------------------------


def test_an_empty_key_is_refused_in_spanish(store):
    with pytest.raises(ServiceError) as exc:
        store.set_api_key("")
    assert "vacía" in str(exc.value)
    assert API_KEY_PREFIX in str(exc.value)


def test_a_whitespace_only_key_is_refused_in_spanish(store):
    with pytest.raises(ServiceError) as exc:
        store.set_api_key("   \n\t ")
    assert "vacía" in str(exc.value)


def test_a_value_that_is_not_an_anthropic_key_is_refused_naming_what_was_expected(store):
    """The wrong-string paste: an OpenAI key, a service token, a URL."""
    for wrong in ("sk-proj-abc123", "token-de-servicio-cualquiera", "https://console.anthropic.com"):
        with pytest.raises(ServiceError) as exc:
            store.set_api_key(wrong)
        assert API_KEY_PREFIX in str(exc.value), wrong
        assert wrong not in str(exc.value), "el mensaje no repite lo pegado"
    assert store.has_api_key() is False


def test_a_refused_key_does_not_overwrite_the_one_already_saved(store):
    store.set_api_key(KEY)
    with pytest.raises(ServiceError):
        store.set_api_key("sk-proj-lo-que-sea")
    assert store.get_api_key() == KEY


def test_a_pasted_key_keeps_no_surrounding_whitespace(store):
    """People paste with a trailing newline. A key with \\n in it 401s."""
    store.set_api_key(f"  {KEY}\n")
    assert store.get_api_key() == KEY


# --- clearing ----------------------------------------------------------------


def test_clearing_the_key_makes_has_api_key_false(store):
    store.set_api_key(KEY)
    store.clear_api_key()
    assert store.has_api_key() is False
    assert store.get_api_key() is None


def test_clearing_a_key_that_was_never_set_is_not_an_error(store):
    store.clear_api_key()
    assert store.has_api_key() is False


def test_clearing_leaves_no_ciphertext_behind(store):
    store.set_api_key(KEY)
    store.clear_api_key()
    if store.path.exists():
        assert "anthropicApiKey" not in store.path.read_text(encoding="utf-8")


# --- file permissions --------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="chmod bits are meaningless on Windows")
def test_the_file_is_owner_only_on_posix(store):
    store.set_api_key(KEY)
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "nt", reason="the icacls path only exists on Windows")
def test_the_store_still_works_on_windows(store):
    """NO mode assertion here, on purpose, and that is the whole point.

    `os.chmod(path, 0o600)` on Windows only toggles the read-only flag -- the
    mode bits it reports back are fabricated by the CRT and say nothing about
    who can open the file. Asserting `st_mode & 0o777 == 0o600` here would pass
    while the file inherited the directory's broad ACLs, which is a green test
    certifying the exact bug it was written to catch. What actually restricts
    the file on Windows is the `icacls /inheritance:r /grant:r` call in
    `settings._restrict_to_user`, and its effect is not visible through
    `os.stat`. So: assert the store works, and let the ACL be verified by
    `icacls` if anyone wants to look.
    """
    store.set_api_key(KEY)
    assert store.get_api_key() == KEY
    assert store.path.exists()


# =============================================================================
# resolution order: env > stored > nothing
# =============================================================================
#
# The direction of the dependency is the decision being pinned here.
# `service/jobs.py` already imports `..config`, so config importing the service
# back would be a cycle. Instead the SERVICE INJECTS: `create_app` hands
# `config.set_stored_key_provider` a callable, and `config.resolve_api_key()`
# owns the ORDER. The engine keeps knowing nothing about HTTP, and the order
# lives in one documented place instead of at every call site.


@pytest.fixture
def config_key(monkeypatch):
    """`CONFIG` with every source of a key neutralised, and restored after.

    `config.py` calls `load_dotenv(override=True)` at import, so a developer's
    real `06_Application/.env` key IS in `os.environ` by the time a test runs.
    Deleting the variable is what makes "a stranger's fresh install" expressible
    at all.
    """
    from lxp_eval import config as config_mod

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config_mod, "_stored_key_provider", None, raising=False)
    return config_mod


def test_with_nothing_configured_the_key_resolves_to_empty(config_key):
    assert config_key.CONFIG.resolve_api_key() == ""


def test_the_stored_key_is_used_when_the_environment_has_none(config_key, store):
    store.set_api_key(KEY)
    config_key.set_stored_key_provider(store.get_api_key)
    assert config_key.CONFIG.resolve_api_key() == KEY


def test_the_environment_variable_wins_over_the_stored_key(config_key, store, monkeypatch):
    """An operator running headless/CI must be able to override the UI."""
    store.set_api_key(KEY)
    config_key.set_stored_key_provider(store.get_api_key)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-de-la-variable-de-entorno")
    assert config_key.CONFIG.resolve_api_key() == "sk-ant-de-la-variable-de-entorno"


def test_a_broken_provider_does_not_take_the_engine_down(config_key):
    """The settings file being unreadable is not a reason to stop resolving."""
    def boom():
        raise OSError("disco lleno")

    config_key.set_stored_key_provider(boom)
    assert config_key.CONFIG.resolve_api_key() == ""


def test_validate_stops_telling_a_stranger_the_env_var_is_the_only_way(config_key):
    errors = "\n".join(config_key.CONFIG.validate())
    assert "ANTHROPIC_API_KEY" in errors, "el modo headless sigue nombrado"
    assert "aplicación" in errors, "y la aplicación es la primera opción"
