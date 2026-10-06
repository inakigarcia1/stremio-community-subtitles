import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "app" / "lib" / "anime_episode_index.py"
_spec = importlib.util.spec_from_file_location("anime_episode_index", _PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
absolute_episode = _module.absolute_episode
filter_absolute_response = _module.filter_absolute_response
keep_absolute_hit = _module.keep_absolute_hit

NARUTO = {0: 7, 1: 35, 2: 48, 3: 48, 4: 48, 5: 41}
JJK = {1: 24, 2: 23, 3: 12}


def test_naruto_s4e1_is_absolute_132():
    assert absolute_episode(NARUTO, 4, 1) == 132


def test_jjk_s2e1_is_absolute_25():
    assert absolute_episode(JJK, 2, 1) == 25


def test_missing_prior_season_does_not_guess():
    assert absolute_episode({1: 10, 3: 10}, 3, 1) is None


def test_keep_only_numbers_past_the_season_length():
    assert keep_absolute_hit(1, 132, NARUTO)
    assert keep_absolute_hit(2, 25, JJK)
    assert keep_absolute_hit(0, 132, NARUTO)
    assert not keep_absolute_hit(1, 30, {1: 40})
    assert not keep_absolute_hit(4, 1, NARUTO)


def test_filter_drops_in_range_episodes():
    payload = {
        "data": [
            {"attributes": {"feature_details": {"season_number": 1, "episode_number": 132}}},
            {"attributes": {"feature_details": {"season_number": 1, "episode_number": 5}}},
        ]
    }
    kept = filter_absolute_response(payload, NARUTO)
    assert len(kept["data"]) == 1
    assert kept["data"][0]["attributes"]["feature_details"]["episode_number"] == 132
