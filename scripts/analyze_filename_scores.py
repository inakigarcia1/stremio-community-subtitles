"""Analyze filename_score distribution for ffsubsync threshold tuning."""
import random
import re
import statistics
from collections import Counter

from rapidfuzz import fuzz


def extract_release_components(name):
    if not name:
        return {"title": "", "season_episode": "", "group": "", "source": "", "resolution": ""}
    name_lower = re.sub(r"\.[a-zA-Z0-9]+$", "", name.lower())
    name_normalized = re.sub(r"[\._\-]+", " ", name_lower)
    season_episode = ""
    for pattern in [r"s(\d+)\s*e(\d+)", r"s(\d+)\s*(\d+)", r"(\d+)x(\d+)"]:
        match = re.search(pattern, name_normalized)
        if match:
            season_episode = f"s{int(match.group(1)):02d}e{int(match.group(2)):02d}"
            break
    source_types_map = {
        "web-dl": "webdl",
        "webdl": "webdl",
        "web dl": "webdl",
        "webrip": "webrip",
        "web": "web",
        "bluray": "bluray",
        "blu-ray": "bluray",
        "blu ray": "bluray",
        "bdrip": "bluray",
        "brrip": "bluray",
        "remux": "remux",
        "hdtv": "hdtv",
        "hdrip": "hdrip",
        "dvdrip": "dvdrip",
        "dvd": "dvd",
        "cam": "cam",
        "ts": "ts",
        "hdcam": "cam",
    }
    source = next((ns for p, ns in source_types_map.items() if p in name_normalized), "")
    resolution = next((res for res in ["2160p", "1080p", "720p", "480p", "4k", "uhd"] if res in name_normalized), "")
    group = ""
    dash_before_bracket = re.search(r"-([a-z0-9]+)\[", name_lower)
    if dash_before_bracket:
        group = dash_before_bracket.group(1)
    else:
        bracket_match = re.search(r"^\[([a-z0-9.]+)\]|\[([a-z0-9.]+)\]$", name_lower)
        if bracket_match:
            group = bracket_match.group(1) or bracket_match.group(2)
        else:
            dash_match = re.search(r"[-\s]([a-z0-9]+)$", name_normalized)
            if dash_match:
                group = dash_match.group(1)
    title = name_normalized
    if season_episode:
        title = re.sub(r"s\d+\s*e?\d+", "", title)
    all_tags = list(source_types_map.keys()) + [
        "2160p",
        "1080p",
        "720p",
        "480p",
        "4k",
        "uhd",
        "x264",
        "h264",
        "x265",
        "hevc",
        "aac",
        "ac3",
        "dts",
        "hdr",
        "dv",
        "10bit",
    ]
    for tag in all_tags:
        title = title.replace(tag, "")
    title = re.sub(r"\s+", " ", title).strip()
    return {
        "title": title,
        "season_episode": season_episode,
        "group": group,
        "source": source,
        "resolution": resolution,
    }


def calculate_filename_similarity(video_filename, subtitle_release_name, is_forced=False):
    if not video_filename or not subtitle_release_name:
        return 0.0
    video_parts = extract_release_components(video_filename)
    subtitle_parts = extract_release_components(subtitle_release_name)
    if not video_parts["title"] or not subtitle_parts["title"]:
        return 0.0
    if video_parts["season_episode"] and subtitle_parts["season_episode"]:
        if video_parts["season_episode"] != subtitle_parts["season_episode"]:
            return 0.0
    source_tiers = {
        "remux": 5,
        "bluray": 4,
        "webdl": 3,
        "webrip": 3,
        "web": 3,
        "hdtv": 2,
        "hdrip": 2,
        "dvdrip": 1,
        "dvd": 1,
        "ts": 0,
        "cam": 0,
    }
    source_score = 0.0
    if video_parts["source"] and subtitle_parts["source"]:
        if video_parts["source"] == subtitle_parts["source"]:
            source_score = 0.3
        else:
            tier_diff = abs(
                source_tiers.get(video_parts["source"], 2) - source_tiers.get(subtitle_parts["source"], 2)
            )
            source_score = 0.25 if tier_diff == 0 else 0.15 if tier_diff == 1 else 0.02
    elif video_parts["source"] and not subtitle_parts["source"]:
        source_score = 0.1
    group_sim = (
        fuzz.ratio(video_parts["group"], subtitle_parts["group"]) / 100.0
        if video_parts["group"] and subtitle_parts["group"]
        else 0.0
    )
    resolution_sim = 0.0
    if video_parts["resolution"] and subtitle_parts["resolution"]:
        resolution_sim = 1.0 if video_parts["resolution"] == subtitle_parts["resolution"] else 0.0
    title_sim = fuzz.token_sort_ratio(video_parts["title"], subtitle_parts["title"]) / 100.0
    if video_parts["season_episode"]:
        score = source_score + (group_sim * 0.4) + (resolution_sim * 0.15) + (title_sim * 0.05)
    else:
        score = source_score + (group_sim * 0.4) + (resolution_sim * 0.15) + (title_sim * 0.15)
    if is_forced or (subtitle_release_name and "forced" in subtitle_release_name.lower()):
        score *= 0.5
    return min(score, 1.0)


def threshold_report(scores, label):
    print(f"\n=== {label} (n={len(scores)}) ===")
    print(
        f"min={min(scores):.4f} max={max(scores):.4f} "
        f"mean={statistics.mean(scores):.4f} median={statistics.median(scores):.4f}"
    )
    print("modes (0.05):", Counter(round(s * 20) / 20 for s in scores).most_common(8))
    for threshold in [0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8]:
        below = sum(1 for s in scores if s < threshold)
        print(f"  thresh {threshold:.2f} -> ffsubsync ON for {below}/{len(scores)} ({100 * below / len(scores):.1f}%)")


def main():
    calc = calculate_filename_similarity
    pairs = []

    pairs += [
        ("Breaking.Bad.S05E14.1080p.BluRay.x264-GROUP.mkv", "Breaking.Bad.S05E14.1080p.BluRay.x264-GROUP.srt"),
        ("The.Matrix.1999.1080p.BluRay.x264-SPARKS.mkv", "The.Matrix.1999.1080p.BluRay.x264-SPARKS.srt"),
    ]

    for g1 in ["NTb", "FLUX", "EVO", "TOMMY", "RARBG", "YTS", "SPARKS", "GECKOS"]:
        for g2 in ["NTb", "FLUX", "EVO", "TOMMY", "RARBG", "YTS", "SPARKS", "GECKOS", "Subscene", "unknown"]:
            pairs.append(
                (
                    f"Better.Call.Saul.S06E13.1080p.WEB-DL.x264-{g1}.mkv",
                    f"Better.Call.Saul.S06E13.1080p.WEB-DL.x264-{g2}",
                )
            )

    for sub in [
        "Breaking Bad S05E14",
        "Breaking.Bad.S05E14.1080p.WEB-DL",
        "Breaking Bad 5x14 Spanish",
        "Breaking.Bad.S05E14.720p.WEB-DL.x264",
    ]:
        pairs.append(("Breaking.Bad.S05E14.1080p.WEB-DL.x264-NTb.mkv", sub))

    random.seed(42)
    for _ in range(500):
        season, episode = random.randint(1, 5), random.randint(1, 15)
        group = random.choice(["NTb", "FLUX", "EVO", "SPARKS", "GalaxyRG", "BONE"])
        title = random.choice(
            [
                "Breaking.Bad",
                "The.Sopranos",
                "Better.Call.Saul",
                "House.of.the.Dragon",
                "Arcane",
                "Shogun",
                "Wednesday",
                "The.Last.of.Us",
            ]
        )
        video = f"{title}.S{season:02d}E{episode:02d}.1080p.WEB-DL.x264-{group}.mkv"
        style = random.random()
        if style < 0.25:
            subtitle = f"{title.replace('.', ' ')} S{season:02d}E{episode:02d}"
        elif style < 0.45:
            subtitle = f"{title}.S{season:02d}E{episode:02d}.1080p.WEB-DL.x264-{group}"
        elif style < 0.65:
            subtitle = f"{title}.S{season:02d}E{episode:02d}.1080p.WEB-DL.x264-{random.choice(['Subscene', 'unknown', 'YIFY', 'Spanish'])}"
        elif style < 0.80:
            subtitle = f"{title.replace('.', ' ')} {season}x{episode}"
        else:
            subtitle = f"{title}.S{season:02d}E{episode:02d}.720p.WEB-DL"
        pairs.append((video, subtitle))

    all_scores = [calc(video, subtitle) for video, subtitle in pairs]
    threshold_report(all_scores, "ALL SCENARIOS")

    monte_carlo = all_scores[-500:]
    threshold_report(monte_carlo, "MONTE CARLO realistic Spanish styles")

    same_group = [calc(f"Show.S01E01.1080p.WEB-DL.x264-{g}.mkv", f"Show.S01E01.1080p.WEB-DL.x264-{g}") for g in ["NTb", "FLUX", "EVO", "SPARKS"]]
    diff_group = [
        calc("Show.S01E01.1080p.WEB-DL.x264-NTb.mkv", f"Show.S01E01.1080p.WEB-DL.x264-{g}")
        for g in ["FLUX", "EVO", "SPARKS", "unknown", "Subscene"]
    ]
    no_group = [
        calc("Show.S01E01.1080p.WEB-DL.x264-NTb.mkv", sub)
        for sub in ["Show.S01E01.1080p.WEB-DL", "Show S01E01", "Show 1x01"]
    ]

    print("\n=== CATEGORY SNAPSHOTS ===")
    print("same_group:", [round(s, 3) for s in same_group])
    print("diff_group:", [round(s, 3) for s in diff_group])
    print("no_group:", [round(s, 3) for s in no_group])

    print("\n=== THEORETICAL CEILINGS (series) ===")
    ceilings = [
        ("perfect + group", "Show.S01E01.1080p.WEB-DL.x264-NTb.mkv", "Show.S01E01.1080p.WEB-DL.x264-NTb"),
        ("perfect, no group", "Show.S01E01.1080p.WEB-DL.x264-NTb.mkv", "Show.S01E01.1080p.WEB-DL"),
        ("minimal name", "Show.S01E01.1080p.WEB-DL.x264-NTb.mkv", "Show S01E01"),
        ("wrong resolution", "Show.S01E01.1080p.WEB-DL.x264-NTb.mkv", "Show.S01E01.720p.WEB-DL.x264-NTb"),
        ("wrong episode", "Show.S01E01.1080p.WEB-DL.x264-NTb.mkv", "Show.S01E02.1080p.WEB-DL.x264-NTb"),
        ("movie perfect", "Movie.2024.1080p.BluRay.x264-SPARKS.mkv", "Movie.2024.1080p.BluRay.x264-SPARKS"),
    ]
    for label, video, subtitle in ceilings:
        print(f"  {label}: {calc(video, subtitle):.4f}")


if __name__ == "__main__":
    main()
