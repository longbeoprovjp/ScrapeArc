"""Ready-made setups on the Customize screen: complete, and every one readable (WCAG AA)."""
import pytest

from minibooru import themes


@pytest.mark.parametrize("name,desc,colors", themes.SETUPS, ids=[n for n, _, _ in themes.SETUPS])
def test_every_setup_is_complete_and_readable(name, desc, colors):
    assert set(colors) == set(themes.ROLES)                              # sets every colour, nothing left over
    assert all(len(v) == 7 and v.startswith("#") for v in colors.values())
    assert themes.problems(colors) == []                                 # text 4.5:1 on its box, borders 3:1


def test_contrast_formula():
    assert round(themes.contrast("#000000", "#ffffff"), 1) == 21.0
    assert round(themes.contrast("#767676", "#ffffff"), 2) == 4.54        # the classic 'just passes' grey
    assert themes.problems({**dict(themes.SETUPS[0][2]), "btnfg": "#2563eb"})   # unreadable button text is caught


def test_setups_offered_on_the_page(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from minibooru import core
    from minibooru.web import create_app
    monkeypatch.setattr(core, "DATA", tmp_path / "d")
    monkeypatch.delenv("MINIBOORU_PASSWORD", raising=False)
    page = TestClient(create_app()).get("/customize").text
    for name, _, _ in themes.SETUPS:
        assert f'"name": "{name}"' in page


def test_every_setup_fades_the_background_picture():
    assert set(themes.FADE) == {n for n, _, _ in themes.SETUPS}
    assert all(0 <= float(v) <= 0.9 for v in themes.FADE.values())
