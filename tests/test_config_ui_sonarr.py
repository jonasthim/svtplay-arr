"""The Test connection control on the Settings view.

`/health` never touched Sonarr, so it reported `ok` through a completely
wrong URL or a mistyped key -- and the only symptom was that episodes
stopped arriving. Every deployment of this service has ended with a human
running a Sonarr search by hand, because nothing automated could confirm
the connection worked. The API key became editable through this page on
2026-08-25, which made it something that can be mistyped from a phone.

Three properties carry the feature, and each is tested here rather than
assumed:

* **It tests the values in the form.** The operator clicks this having just
  typed a key, to find out whether it works *before* saving it. Testing the
  file could not answer that, and since settings need a restart the file is
  not what the running service is using either.
* **Every failure shape arrives distinguishably.** A refused port, an
  unknown host, an unverifiable certificate, a rejected key and a proxy
  answering in Sonarr's place send the operator to five different places.
* **The API key never appears in any output, on any path.** httpx hangs the
  whole `Request` -- headers included -- off its exceptions, so this is one
  careless `str(exc)` away from being false.
"""

import asyncio
import html as html_mod
import inspect
import re
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from svtplay_arr.api.config_ui import build_config_router
from svtplay_arr.config import Settings
from svtplay_arr.mappings import add_mapping
from svtplay_arr.sonarr import (
    REASON_BAD_URL,
    REASON_HTTP,
    REASON_MESSAGES,
    REASON_NOT_SONARR,
    REASON_REFUSED,
    REASON_TLS,
    REASON_UNAUTHORIZED,
    REASON_UNREACHABLE,
    SonarrApiError,
    SonarrStatus,
)

TITLE = "Gift vid första ögonkastet"
KEY = "SECRET-KEY-VALUE"


class FakeSvt:
    async def search_series(self, query):
        return []

    async def list_episodes(self, slug):
        return []


class FakeSonarr:
    async def all_series(self):
        return []


class Probe:
    """Stands in for `app.create_app`'s `probe_sonarr`.

    Records what it was asked to test, which is how "does this check the
    form or the file" is answered structurally rather than by reading the
    rendered prose.
    """

    def __init__(self, result=None, error=None, hang=False):
        self.result = result or SonarrStatus(
            version="4.0.10.2544", series_count=42
        )
        self.error = error
        self.hang = hang
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, url, api_key):
        self.calls.append((url, api_key))
        if self.hang:
            await asyncio.sleep(3600)
        if self.error is not None:
            raise self.error
        return self.result


def _paths(tmp_path: Path):
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.test:8989\n"
        f"sonarr_api_key: {KEY}\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n",
        encoding="utf-8",
    )
    maps = tmp_path / "mappings.yaml"
    # Idempotent: several of these tests build more than one client against
    # the same tmp_path, and a second add_mapping would refuse the duplicate.
    if not maps.exists():
        add_mapping(
            maps, tvdb_id=288649, svt_series_id="jpmQD3q",
            svt_slug="gift-vid-forsta-ogonkastet", series_title=TITLE,
            expected_mtime=None,
        )
    return cfg, maps


def _client(tmp_path: Path, **providers) -> TestClient:
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(cfg, maps, FakeSvt(), FakeSonarr(), **providers)
    )
    return TestClient(app)


def _form(tmp_path: Path, **over):
    values = {
        "sonarr_url": "http://sonarr.test:8989",
        "sonarr_api_key": KEY,
        "incomplete_dir": f"{tmp_path}/i",
        "completed_dir": f"{tmp_path}/c",
        "air_date_tolerance_days": "1",
        "rss_window_days": "7",
        "max_concurrent_downloads": "1",
    }
    values.update(over)
    return values


def _result_text(html: str) -> str:
    m = re.search(r'<p class="sonarr-test-result[^"]*"[^>]*>(.*?)</p>', html, re.S)
    assert m, f"no test result rendered in:\n{html}"
    # Unescaped, because the messages carry apostrophes ("Sonarr's API") and
    # a test that matched the escaped form would be asserting on Jinja's
    # autoescaping rather than on what the operator reads.
    return html_mod.unescape(m.group(1))


def _result_class(html: str) -> str:
    m = re.search(r'<p class="(sonarr-test-result[^"]*)"', html)
    assert m, f"no test result rendered in:\n{html}"
    return m.group(1)


def _post_with_deadline(client, path, deadline=10.0, **kwargs):
    """POST with a deadline of its own, for any test driving a hanging probe.

    Without the bound in `_sonarr_test` these requests never return, and a
    test that simply awaited one would wedge the entire run rather than
    failing -- which is worth nothing in CI, and is exactly the defect this
    helper exists to stop recurring. Every test that hands the router a
    `Probe(hang=True)` goes through here, not just the one where the
    problem was first noticed.

    The thread is a daemon, so a hung request is abandoned rather than
    holding the session open.
    """
    done: list = []
    thread = threading.Thread(
        target=lambda: done.append(client.post(path, **kwargs)), daemon=True
    )
    thread.start()
    thread.join(timeout=deadline)
    assert not thread.is_alive(), (
        f"POST {path} never returned within {deadline}s -- the Sonarr call "
        "is unbounded"
    )
    (response,) = done
    return response


def _sonarr_error(reason: str) -> SonarrApiError:
    """Exactly what `SonarrClient` raises for that shape.

    Built through the client's own message table, so a test cannot pass
    against wording no real failure would ever carry.
    """
    return SonarrApiError(REASON_MESSAGES[reason], reason=reason)


# --- The control exists, and only where it can work ------------------------


def test_the_settings_view_offers_a_test_connection_button(tmp_path: Path):
    body = _client(tmp_path, sonarr_probe=Probe()).get("/config/settings").text
    assert 'class="sonarr-test-button"' in body
    assert 'formaction="/config/settings/test"' in body


def test_no_button_is_rendered_where_nothing_could_answer_it(tmp_path: Path):
    # Same rule as the Show/Hide toggle: a control that cannot do anything
    # is worse than no control, because the operator reads its silence as an
    # answer.
    body = _client(tmp_path).get("/config/settings").text
    # The form only -- the inline script names the class unconditionally,
    # which is fine: it enhances a button that is not there, so it finds
    # nothing and does nothing.
    form = body[body.index('<form method="post" action="/config/settings">'):
                body.index("</form>")]
    assert "sonarr-test-button" not in form


def test_the_control_is_a_plain_form_post_that_needs_no_javascript(
    tmp_path: Path,
):
    # The whole page works with JavaScript off, and this is not the control
    # that breaks that. A submit button with its own `formaction` inside the
    # settings form is an ordinary POST; the fetch enhancement in base.html
    # only intercepts it.
    body = _client(tmp_path, sonarr_probe=Probe()).get("/config/settings").text
    assert re.search(
        r'<button type="submit" class="sonarr-test-button"\s+'
        r'formaction="/config/settings/test"\s+formnovalidate>',
        body,
    ), body


def test_save_is_still_what_pressing_enter_in_a_field_does(tmp_path: Path):
    # Implicit submission activates the *first* submit button in tree order,
    # and Test connection now sits above Save settings in this form. Without
    # the hidden default button, adding this control would silently change
    # Enter from "save my settings" to "run a connection test".
    body = _client(tmp_path, sonarr_probe=Probe()).get("/config/settings").text
    form = body[body.index('<form method="post" action="/config/settings">'):]
    submits = re.findall(r'<button type="submit"[^>]*>', form)
    assert submits, "no submit buttons in the settings form"
    assert "sonarr-test-button" not in submits[0], (
        "Test connection is the form's default button, so Enter in a field "
        "would test instead of saving"
    )
    assert "default-submit" in submits[0]


def test_every_route_including_the_new_one_is_async(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    router = build_config_router(
        cfg, maps, FakeSvt(), FakeSonarr(), sonarr_probe=Probe()
    )
    paths = [r.path for r in router.routes if hasattr(r, "endpoint")]
    assert "/config/settings/test" in paths
    for route in router.routes:
        if hasattr(route, "endpoint"):
            assert inspect.iscoroutinefunction(route.endpoint), route.path


# --- Form, not file --------------------------------------------------------


def test_the_test_uses_the_values_in_the_form_not_the_ones_on_disk(
    tmp_path: Path,
):
    # The decision the control turns on. The operator has just typed a key
    # and wants to know before saving it; the file cannot answer that, and
    # after a save it still cannot, because settings need a restart.
    probe = Probe()
    r = _client(tmp_path, sonarr_probe=probe).post(
        "/config/settings/test",
        data=_form(
            tmp_path,
            sonarr_url="http://other.sonarr:8989",
            sonarr_api_key="TYPED-IN-THE-BROWSER",
        ),
    )
    assert r.status_code == 200
    assert probe.calls == [("http://other.sonarr:8989", "TYPED-IN-THE-BROWSER")]


def test_the_test_writes_nothing(tmp_path: Path):
    # Read-only is a hard requirement: this is a button an operator presses
    # while mid-edit, with values they have not committed to.
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(), sonarr_probe=Probe()
        )
    )
    before = {
        p: (p.stat().st_mtime_ns, p.read_bytes())
        for p in tmp_path.rglob("*") if p.is_file()
    }
    with TestClient(app) as c:
        c.post(
            "/config/settings/test",
            data=_form(tmp_path, sonarr_url="http://elsewhere:8989"),
        )
    after = {
        p: (p.stat().st_mtime_ns, p.read_bytes())
        for p in tmp_path.rglob("*") if p.is_file()
    }
    assert after == before


def test_a_rejected_test_leaves_the_typed_values_in_the_boxes(tmp_path: Path):
    # The operator has not saved yet. A control that discards their work in
    # order to tell them something went wrong is a control they will stop
    # pressing.
    probe = Probe(error=_sonarr_error(REASON_UNAUTHORIZED))
    body = _client(tmp_path, sonarr_probe=probe).post(
        "/config/settings/test",
        data=_form(tmp_path, sonarr_url="http://typed.sonarr:8989"),
    ).text
    assert 'value="http://typed.sonarr:8989"' in body


def test_a_get_of_the_settings_view_never_calls_sonarr(tmp_path: Path):
    # Strictly on demand, exactly like the mapping Check control: no page
    # render may reach out to Sonarr, or every reload costs two requests and
    # a slow Sonarr makes the page unusable.
    probe = Probe()
    client = _client(tmp_path, sonarr_probe=probe)
    client.get("/config/settings")
    client.get("/config")
    client.get("/config/mappings")
    assert probe.calls == []


# --- What a success says ---------------------------------------------------


def test_a_successful_test_reports_the_version_and_the_series_count(
    tmp_path: Path,
):
    # The count is the part that matters: reachable and authenticated are
    # both satisfied by a Sonarr that simply is not the one this service is
    # meant to feed.
    body = _client(tmp_path, sonarr_probe=Probe()).post(
        "/config/settings/test", data=_form(tmp_path)
    ).text
    result = _result_text(body)
    assert "4.0.10.2544" in result
    assert "42 series" in result
    assert "notice" in _result_class(body)


def test_an_empty_library_is_a_success_that_still_says_to_look_twice(
    tmp_path: Path,
):
    probe = Probe(result=SonarrStatus(version="4.0.10.2544", series_count=0))
    body = _client(tmp_path, sonarr_probe=probe).post(
        "/config/settings/test", data=_form(tmp_path)
    ).text
    result = _result_text(body)
    assert "0 series" in result
    assert "notice" in _result_class(body)
    assert "wrong one" in result


# --- Telling the failures apart --------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        REASON_BAD_URL, REASON_UNREACHABLE, REASON_REFUSED, REASON_TLS,
        REASON_UNAUTHORIZED, REASON_NOT_SONARR, REASON_HTTP,
    ],
)
def test_each_failure_shape_reports_its_own_sentence(tmp_path: Path, reason):
    # Collapsing any two of these into "Sonarr could not be reached" is what
    # would make the control worthless: the fix for a rejected key and the
    # fix for a wrong port have nothing in common.
    probe = Probe(error=_sonarr_error(reason))
    body = _client(tmp_path, sonarr_probe=probe).post(
        "/config/settings/test", data=_form(tmp_path)
    ).text
    assert REASON_MESSAGES[reason].split(".")[0] in _result_text(body)
    assert "error" in _result_class(body)


def test_the_failure_sentences_are_actually_different_from_each_other(
    tmp_path: Path,
):
    # The parametrised test above would still pass if every reason mapped to
    # the same sentence, since each would trivially contain itself.
    rendered = set()
    for reason in REASON_MESSAGES:
        probe = Probe(error=_sonarr_error(reason))
        body = _client(tmp_path, sonarr_probe=probe).post(
            "/config/settings/test", data=_form(tmp_path)
        ).text
        rendered.add(_result_text(body).strip())
    assert len(rendered) == len(REASON_MESSAGES)


def test_a_blank_url_or_key_is_refused_without_calling_sonarr(tmp_path: Path):
    # "Sonarr rejected the key" about an empty box would send the operator
    # to Sonarr for a problem that is on this page.
    probe = Probe()
    client = _client(tmp_path, sonarr_probe=probe)
    for over in ({"sonarr_url": "  "}, {"sonarr_api_key": ""}):
        body = client.post(
            "/config/settings/test", data=_form(tmp_path, **over)
        ).text
        assert "Nothing was sent" in _result_text(body)
        assert "warn" in _result_class(body)
    assert probe.calls == []


def test_the_environment_key_is_used_for_the_url_this_host_is_configured_for(
    tmp_path: Path, monkeypatch
):
    # $SONARR_API_KEY beats config.yaml in Settings.load, so on such a
    # deployment the typed value is not what any restart would use. Testing
    # it would report on a value that can never take effect -- and the
    # result has to say which key was actually used, or it is misleading in
    # the other direction.
    #
    # The URL here is the one already in config.yaml, which is where the
    # running service sends that key on every RSS poll anyway.
    monkeypatch.setenv("SONARR_API_KEY", "FROM-THE-ENVIRONMENT")
    probe = Probe()
    body = _client(tmp_path, sonarr_probe=probe).post(
        "/config/settings/test",
        data=_form(tmp_path, sonarr_api_key="TYPED-IN-THE-BROWSER"),
    ).text
    assert probe.calls == [("http://sonarr.test:8989", "FROM-THE-ENVIRONMENT")]
    assert "SONARR_API_KEY" in _result_text(body)


def test_the_environment_key_is_never_sent_to_a_url_from_the_form(
    tmp_path: Path, monkeypatch
):
    # The one that matters. The URL comes from the submitted body and the
    # key would come from the environment, so an unrestricted substitution
    # hands a secret this page deliberately never renders to whatever host
    # the request names -- immediately, with no config write, no restart and
    # no log line carrying the value. There is no CSRF token on this form
    # and no Origin check in this service, so it is reachable cross-site,
    # and SECURITY.md publishes the opposite as a guarantee.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-ONLY-SECRET-NEVER-RENDERED")
    probe = Probe()
    body = _client(tmp_path, sonarr_probe=probe).post(
        "/config/settings/test",
        data=_form(
            tmp_path,
            sonarr_url="http://attacker.invalid:9999",
            sonarr_api_key="ANY-JUNK",
        ),
    ).text
    assert probe.calls == [("http://attacker.invalid:9999", "ANY-JUNK")]
    assert "ENV-ONLY-SECRET-NEVER-RENDERED" not in repr(probe.calls)
    assert "ENV-ONLY-SECRET-NEVER-RENDERED" not in body
    # ...and it says so, rather than reporting success as though the
    # environment's key had been tried.
    result = _result_text(body)
    assert "was not sent" in result
    assert "after a restart" in result


def test_the_environment_key_is_never_sent_to_a_url_from_the_form_on_failure(
    tmp_path: Path, monkeypatch
):
    # "Sonarr answered and rejected the API key" is actively misleading if
    # the operator believes the environment's key was the one tried, so the
    # note rides on the failure paths too, not only on success.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-ONLY-SECRET-NEVER-RENDERED")
    probe = Probe(error=_sonarr_error(REASON_UNAUTHORIZED))
    body = _client(tmp_path, sonarr_probe=probe).post(
        "/config/settings/test",
        data=_form(
            tmp_path,
            sonarr_url="http://attacker.invalid:9999",
            sonarr_api_key="ANY-JUNK",
        ),
    ).text
    assert probe.calls == [("http://attacker.invalid:9999", "ANY-JUNK")]
    assert "ENV-ONLY-SECRET-NEVER-RENDERED" not in body
    assert "was not sent" in _result_text(body)


def test_a_blank_key_against_an_unconfigured_url_sends_nothing_at_all(
    tmp_path: Path, monkeypatch
):
    # The obvious way round the guard: leave the key box empty and hope the
    # environment fills it in. It does not, and nothing is sent.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-ONLY-SECRET-NEVER-RENDERED")
    probe = Probe()
    body = _client(tmp_path, sonarr_probe=probe).post(
        "/config/settings/test",
        data=_form(
            tmp_path,
            sonarr_url="http://attacker.invalid:9999",
            sonarr_api_key="",
        ),
    ).text
    assert probe.calls == []
    assert "ENV-ONLY-SECRET-NEVER-RENDERED" not in body
    assert "Nothing was sent" in _result_text(body)


def test_the_url_the_service_booted_with_is_trusted_after_a_save(
    tmp_path: Path, monkeypatch
):
    # Between a save and the restart that applies it, the file and the
    # running service disagree about the Sonarr URL -- and *both* are
    # values the operator committed to on this host. The running one is
    # where the environment's key is already going on every RSS poll, so
    # refusing to test it would leave the very URL in use untestable.
    #
    # Dropping `booted` from the trusted set survived every other test,
    # because every fixture has the file and the boot agreeing.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-ONLY-SECRET-NEVER-RENDERED")
    cfg, maps = _paths(tmp_path)
    booted = Settings(
        sonarr_url="http://booted.sonarr:8989",
        sonarr_api_key="IRRELEVANT",
        incomplete_dir=tmp_path / "i",
        completed_dir=tmp_path / "c",
        config_path=cfg,
    )
    # ...and the file now says somewhere else, as it would after a save.
    cfg.write_text(
        cfg.read_text().replace(
            "sonarr_url: http://sonarr.test:8989",
            "sonarr_url: http://saved.sonarr:8989",
        ),
        encoding="utf-8",
    )

    probe = Probe()
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            booted=booted, sonarr_probe=probe,
        )
    )
    client = TestClient(app)
    for url in (
        "http://booted.sonarr:8989",   # what the service is running on
        "http://saved.sonarr:8989",    # what the next restart will use
        "http://attacker.invalid",     # neither
    ):
        client.post(
            "/config/settings/test",
            data=_form(tmp_path, sonarr_url=url, sonarr_api_key="JUNK"),
        )

    assert probe.calls == [
        ("http://booted.sonarr:8989", "ENV-ONLY-SECRET-NEVER-RENDERED"),
        ("http://saved.sonarr:8989", "ENV-ONLY-SECRET-NEVER-RENDERED"),
        ("http://attacker.invalid", "JUNK"),
    ]


def test_a_trailing_slash_is_the_same_url_and_a_different_host_is_not(
    tmp_path: Path, monkeypatch
):
    # `SonarrClient` strips the trailing slash itself, so the two spellings
    # are genuinely one host and refusing the substitution there would be a
    # gratuitous inconsistency. Everything else is compared strictly: being
    # wrong towards "trusted" is the failure this guard exists to prevent,
    # and being wrong the other way only tests the submitted key.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-ONLY-SECRET-NEVER-RENDERED")
    probe = Probe()
    client = _client(tmp_path, sonarr_probe=probe)

    client.post(
        "/config/settings/test",
        data=_form(tmp_path, sonarr_url="http://sonarr.test:8989/",
                   sonarr_api_key="JUNK"),
    )
    # A host that merely starts the same is not the same host.
    client.post(
        "/config/settings/test",
        data=_form(tmp_path, sonarr_url="http://sonarr.test:8989.evil.invalid",
                   sonarr_api_key="JUNK"),
    )
    assert probe.calls == [
        ("http://sonarr.test:8989/", "ENV-ONLY-SECRET-NEVER-RENDERED"),
        ("http://sonarr.test:8989.evil.invalid", "JUNK"),
    ]


# --- Never a 500, never a hang ---------------------------------------------


def test_a_hanging_sonarr_does_not_hang_the_page(tmp_path: Path, monkeypatch):
    # Every route here is `async def`, so an unbounded await holds the event
    # loop the download worker runs on. A Sonarr that accepts the connection
    # and then says nothing must cost this click its timeout and no more.
    #
    # Driven through `_post_with_deadline` so the unbounded case fails
    # cleanly rather than wedging the run; see that helper.
    monkeypatch.setattr(
        "svtplay_arr.api.config_ui._SONARR_TEST_TIMEOUT_S", 0.05
    )
    client = _client(tmp_path, sonarr_probe=Probe(hang=True))
    r = _post_with_deadline(
        client, "/config/settings/test", data=_form(tmp_path)
    )
    assert r.status_code == 200
    assert "did not answer within" in _result_text(r.text)


def test_a_probe_that_explodes_renders_an_error_rather_than_a_500(
    tmp_path: Path,
):
    async def boom(url, api_key):
        raise RuntimeError("something nobody anticipated")

    r = _client(tmp_path, sonarr_probe=boom).post(
        "/config/settings/test", data=_form(tmp_path)
    )
    assert r.status_code == 200
    # This module's own words, not the exception's: an unexpected type must
    # not be able to smuggle whatever it is carrying onto the page.
    assert "something nobody anticipated" not in r.text
    assert "failed unexpectedly" in _result_text(r.text)


def test_a_test_posted_without_a_probe_wired_in_says_so(tmp_path: Path):
    r = _client(tmp_path).post("/config/settings/test", data=_form(tmp_path))
    assert r.status_code == 200
    assert "not available" in _result_text(r.text)


# --- The JSON path is the same computation ---------------------------------


def test_the_json_response_is_the_same_result_the_page_renders(tmp_path: Path):
    # Two response shapes over one computation, exactly as the mapping Check
    # control does it -- so the enhanced control can never disagree with a
    # full page reload.
    client = _client(tmp_path, sonarr_probe=Probe())
    data = _form(tmp_path)
    payload = client.post(
        "/config/settings/test", data=data,
        headers={"Accept": "application/json"},
    ).json()
    page = client.post("/config/settings/test", data=data).text
    assert payload["outcome"] == "ok"
    assert payload["css_class"] == "notice"
    assert payload["series_count"] == 42
    assert payload["version"] == "4.0.10.2544"
    assert payload["message"].strip() in " ".join(_result_text(page).split())


def test_the_json_response_carries_the_reason_a_failure_had(tmp_path: Path):
    probe = Probe(error=_sonarr_error(REASON_UNAUTHORIZED))
    payload = _client(tmp_path, sonarr_probe=probe).post(
        "/config/settings/test", data=_form(tmp_path),
        headers={"Accept": "application/json"},
    ).json()
    assert payload["outcome"] == "failed"
    assert payload["reason"] == REASON_UNAUTHORIZED
    assert payload["css_class"] == "error"


# --- The key, on every path ------------------------------------------------


def test_the_api_key_never_appears_in_any_test_result(tmp_path: Path, monkeypatch):
    # The constraint the whole feature turns on, walked over every outcome
    # rather than the one that was easiest to write: success, every
    # classified failure, a timeout, an unexpected exception carrying the
    # key in its own message, and the refused-before-sending path.
    #
    # The rendered *page* still contains the key -- it is the settings form,
    # and the field holds it by design. What must never contain it is the
    # test result, which is what a screenshot, a support paste or a JSON
    # response carries on its own.
    # A short timeout so the hanging probe below costs this test 50ms rather
    # than the control's real ten seconds, twice.
    monkeypatch.setattr(
        "svtplay_arr.api.config_ui._SONARR_TEST_TIMEOUT_S", 0.05
    )
    leaked = f"X-Api-Key: {KEY}"

    async def leaky(url, api_key):
        raise RuntimeError(leaked)

    probes = [Probe(), Probe(hang=True), leaky]
    probes += [Probe(error=_sonarr_error(r)) for r in REASON_MESSAGES]
    for probe in probes:
        client = _client(tmp_path, sonarr_probe=probe)
        # Deadline-bounded, because one of these probes hangs: `bf49efb`
        # fixed the dedicated timeout test and left this one able to wedge
        # the run under the very same mutation.
        payload = _post_with_deadline(
            client, "/config/settings/test", data=_form(tmp_path),
            headers={"Accept": "application/json"},
        ).json()
        assert KEY not in repr(payload), payload
        page = _post_with_deadline(
            client, "/config/settings/test", data=_form(tmp_path)
        ).text
        assert KEY not in _result_text(page)


def test_a_real_httpx_failure_never_carries_the_key_into_the_result(
    tmp_path: Path,
):
    # The paranoid one, and the reason it exists: httpx hangs the whole
    # `Request` -- headers, and therefore the key -- off its exceptions.
    # `str()` of an httpx error does not include them, which was confirmed
    # rather than assumed; this pins that the *rendered* path stays true
    # even when the exception really did come from httpx with the key on it.
    import httpx

    from svtplay_arr.sonarr import SonarrClient

    def refuse(request):
        raise httpx.ConnectError(
            "All connection attempts failed", request=request
        ) from ConnectionRefusedError(111, "Connection refused")

    async def probe(url, api_key):
        return await SonarrClient(
            url, api_key, httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        ).status()

    client = _client(tmp_path, sonarr_probe=probe)
    payload = client.post(
        "/config/settings/test", data=_form(tmp_path),
        headers={"Accept": "application/json"},
    ).json()
    assert payload["reason"] == REASON_REFUSED
    assert KEY not in repr(payload)
    assert KEY not in _result_text(
        client.post("/config/settings/test", data=_form(tmp_path)).text
    )


def test_the_result_reuses_only_already_contrast_checked_message_classes(
    tmp_path: Path,
):
    # The theme suite pins the contrast of .error/.notice/.warn in both
    # palettes and pins that each carries a distinguishing glyph, so colour
    # is never the only signal. A new outcome introducing a colour of its
    # own would sit outside all of that. Nothing here defines a fifth state
    # style; the outcomes map onto the four that exist.
    from svtplay_arr.api.config_ui import _SONARR_TEST_CSS_CLASS

    assert set(_SONARR_TEST_CSS_CLASS.values()) <= {"error", "notice", "warn"}
    # ...and every outcome the computation can return has an entry, so none
    # of them falls back to the dict's default and reads as an error.
    outcomes = {"ok", "failed", "incomplete", "unavailable"}
    assert outcomes <= set(_SONARR_TEST_CSS_CLASS)
