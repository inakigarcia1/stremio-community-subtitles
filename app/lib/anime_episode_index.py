"""TVDB season/episode -> absolute episode, for anime OpenSubtitles indexes that way."""


def absolute_episode(counts, season, episode):
    """Sum of episodes before this season, plus the episode. None if a prior season is missing."""
    try:
        season = int(season)
        episode = int(episode)
    except (TypeError, ValueError):
        return None
    if season < 1 or episode < 1:
        return None
    total = 0
    for prior in range(1, season):
        count = counts.get(prior, 0)
        if count < 1:
            return None
        total += count
    return total + episode


def keep_absolute_hit(result_season, result_episode, counts):
    """True when OpenSubtitles stored an absolute number past that season's real length."""
    try:
        season = int(result_season)
        episode = int(result_episode)
    except (TypeError, ValueError):
        return False
    cap = counts.get(season, 0)
    return cap > 0 and episode > cap


def filter_absolute_response(api_response, counts):
    if not api_response:
        return api_response
    data = []
    for item in api_response.get("data") or []:
        feature = ((item.get("attributes") or {}).get("feature_details") or {})
        if keep_absolute_hit(feature.get("season_number"), feature.get("episode_number"), counts):
            data.append(item)
    return {**api_response, "data": data}
