from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RecommendationProfile:
    id: str
    name: str
    media_types: tuple[str, ...]
    preferred_genres: tuple[str, ...] = ()
    preferred_tags: tuple[str, ...] = ()
    required_terms: tuple[str, ...] = ()
    excluded_genres: tuple[str, ...] = ()
    min_runtime_minutes: int | None = None
    max_runtime_minutes: int | None = None
    min_items: int = 1
    language_rule: str = "none"
    description: str = ""
    weights: dict[str, int] = field(default_factory=dict)


BUILT_IN_PROFILES: dict[str, RecommendationProfile] = {
    "anime-series": RecommendationProfile(
        id="anime-series",
        name="Anime Series",
        media_types=("series",),
        preferred_genres=("anime", "animation"),
        preferred_tags=("anime",),
        required_terms=("anime",),
        min_runtime_minutes=15,
        max_runtime_minutes=35,
        min_items=3,
        language_rule="english_audio_or_subtitles",
        description="Series-style anime blocks with configurable English dub/sub safety.",
        weights={"genre": 30, "runtime": 20, "depth": 20, "language": 30},
    ),
    "anime-movies": RecommendationProfile(
        id="anime-movies",
        name="Anime Movies/OVAs",
        media_types=("movie",),
        preferred_genres=("anime", "animation"),
        preferred_tags=("anime", "ova"),
        required_terms=("anime",),
        min_runtime_minutes=40,
        max_runtime_minutes=180,
        language_rule="english_audio_or_subtitles",
        description="Anime movies, OVAs, and longer specials.",
        weights={"genre": 35, "runtime": 25, "language": 30, "metadata": 10},
    ),
    "morning-sitcoms": RecommendationProfile(
        id="morning-sitcoms",
        name="Morning Sitcoms",
        media_types=("series",),
        preferred_genres=("comedy", "family", "sitcom"),
        preferred_tags=("sitcom", "light"),
        min_runtime_minutes=18,
        max_runtime_minutes=35,
        min_items=6,
        description="Short light series that fit morning rotation blocks.",
        weights={"genre": 35, "runtime": 25, "depth": 25, "metadata": 15},
    ),
    "prime-time-movies": RecommendationProfile(
        id="prime-time-movies",
        name="Prime-Time Movies",
        media_types=("movie",),
        preferred_genres=("action", "adventure", "drama", "sci-fi", "thriller"),
        min_runtime_minutes=75,
        max_runtime_minutes=160,
        description="Feature-length movies that fit prime-time movie blocks.",
        weights={"runtime": 35, "genre": 25, "metadata": 20, "rating": 20},
    ),
    "afternoon-family": RecommendationProfile(
        id="afternoon-family",
        name="Afternoon Family/Light TV",
        media_types=("series", "movie"),
        preferred_genres=("family", "comedy", "adventure", "animation"),
        preferred_tags=("family", "light", "afternoon"),
        excluded_genres=("horror", "erotic"),
        min_runtime_minutes=18,
        max_runtime_minutes=120,
        min_items=4,
        description="Accessible afternoon programming with family and light-TV bias.",
        weights={"genre": 35, "runtime": 20, "depth": 15, "metadata": 15, "rating": 10},
    ),
    "late-night-genre": RecommendationProfile(
        id="late-night-genre",
        name="Late Night Crime/Sci-Fi/Horror",
        media_types=("series", "movie"),
        preferred_genres=("crime", "sci-fi", "science fiction", "horror", "thriller"),
        preferred_tags=("late night", "crime", "sci-fi", "horror"),
        min_runtime_minutes=20,
        max_runtime_minutes=140,
        min_items=3,
        description="Darker genre content for late-night schedules.",
        weights={"genre": 40, "runtime": 20, "depth": 15, "metadata": 15, "rating": 10},
    ),
    "kids-family": RecommendationProfile(
        id="kids-family",
        name="Kids/Family",
        media_types=("series", "movie"),
        preferred_genres=("kids", "children", "family", "animation"),
        preferred_tags=("kids", "children", "family"),
        required_terms=("kids", "children", "family", "animation"),
        excluded_genres=("horror", "thriller", "crime", "erotic"),
        min_runtime_minutes=5,
        max_runtime_minutes=120,
        min_items=3,
        description="Child- and family-oriented content pools.",
        weights={"genre": 45, "runtime": 15, "depth": 15, "metadata": 15, "rating": 10},
    ),
    "documentary": RecommendationProfile(
        id="documentary",
        name="Documentary",
        media_types=("series", "movie"),
        preferred_genres=("documentary", "history", "nature", "science", "travel"),
        preferred_tags=("documentary", "docs", "real life"),
        required_terms=("documentary", "docs"),
        min_runtime_minutes=20,
        max_runtime_minutes=180,
        min_items=2,
        description="Documentary films and factual series.",
        weights={"genre": 45, "runtime": 20, "depth": 15, "metadata": 15, "rating": 5},
    ),
    "series-marathon": RecommendationProfile(
        id="series-marathon",
        name="Series Marathon",
        media_types=("series",),
        preferred_genres=("drama", "comedy", "crime", "sci-fi", "adventure"),
        min_runtime_minutes=15,
        max_runtime_minutes=70,
        min_items=12,
        description="Series with enough episode depth for long continuous runs.",
        weights={"depth": 40, "runtime": 25, "genre": 20, "metadata": 15},
    ),
    "movie-channel-pool": RecommendationProfile(
        id="movie-channel-pool",
        name="Movie Channel Pool",
        media_types=("movie",),
        preferred_genres=("action", "adventure", "comedy", "drama", "thriller", "sci-fi"),
        min_runtime_minutes=60,
        max_runtime_minutes=180,
        description="Broad movie pool for random or daypart-based movie channels.",
        weights={"runtime": 30, "genre": 25, "metadata": 20, "rating": 15},
    ),
    "standby-off-air": RecommendationProfile(
        id="standby-off-air",
        name="Standby/Off-Air Loop",
        media_types=("series", "movie"),
        preferred_genres=("ambient", "documentary", "music", "short", "animation"),
        preferred_tags=("standby", "off-air", "loop", "filler", "short"),
        max_runtime_minutes=90,
        min_items=1,
        description="Calm or repeatable content suitable for standby and off-air loops.",
        weights={"genre": 30, "runtime": 25, "depth": 15, "metadata": 10},
    ),
    "holiday-event": RecommendationProfile(
        id="holiday-event",
        name="Holiday/Event Programming",
        media_types=("series", "movie"),
        preferred_genres=("holiday", "christmas", "halloween", "family", "animation"),
        preferred_tags=("holiday", "christmas", "halloween", "seasonal", "event"),
        required_terms=("holiday", "christmas", "halloween", "seasonal", "event"),
        min_runtime_minutes=5,
        max_runtime_minutes=180,
        min_items=1,
        description="Seasonal and event-specific programming pools.",
        weights={"genre": 45, "runtime": 15, "depth": 10, "metadata": 20, "rating": 5},
    ),
}
