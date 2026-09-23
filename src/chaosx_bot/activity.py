"""Chaos-tier member activity.

Members are ranked on the mod's own chaos meter: XP accumulates from real activity in the archive and the
tier names are the mod's chaos tiers, thresholds and all. Source of truth in the mod repo:
`common/script_constants/chaos_meter_constants.txt` (`chaos_meter_tier_range`: 0/200/400/600/800/1000)
and `localisation/english/chaosx_chaos_meter_l_english.yml` (`chaos_tier_0`..`chaos_tier_final`):
Calm World, Gathering Storm, Rising Chaos, Chaos Tier, Total Chaos, World Collapse.
Hoops asked for "chaos tiers named levels" - this is that ladder.

Everything here is pure: the rollup reads `message_archive`, so results are recomputable and a formula
change never needs a migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

# The mod's chaos meter, in order: names and thresholds are the mod's own values.
TIERS: tuple[tuple[str, int], ...] = (
    ("Calm World", 0),
    ("Gathering Storm", 200),
    ("Rising Chaos", 400),
    ("Chaos Tier", 600),
    ("Total Chaos", 800),
    ("World Collapse", 1000),
)

# The mod colours each tier in its meter localisation with HOI4 colour codes:
# chaos_tier_0 = §G, tier_1 = §Y, tier_2 = §O, tier_3 = §R, tier_4/tier_final = §0 (dark), and the
# meter value follows the same ramp. These are the Discord equivalents, kept readable as a name
# colour (the mod's §0 is near-black, so the two darkest tiers use dark reds instead).
TIER_COLORS: dict[str, int] = {
    "Calm World": 0x4C9C2E,
    "Gathering Storm": 0xE3C000,
    "Rising Chaos": 0xF08C1F,
    "Chaos Tier": 0xD6453C,
    "Total Chaos": 0x7A1B1B,
    "World Collapse": 0x3B0B0B,
}

# The mod's own ramp, one emoji per tier (green → storm → fire → whirlwind → eruption → eclipse).
# Hoops rejected the skulls (2026-09-23), so the escalation stays elemental all the way up.
TIER_EMOJI: dict[str, str] = {
    "Calm World": "🌿",
    "Gathering Storm": "🌩️",
    "Rising Chaos": "🔥",
    "Chaos Tier": "🌪️",
    "Total Chaos": "🌋",
    "World Collapse": "🌑",
}

# "High level active members" (Hoops 2026-09-23): only these can ever be picked for idle banter.
DEFAULT_ELIGIBLE_TIER = "Rising Chaos"

# XP rules. Chat is regulated so volume cannot buy rank (Hoops 2026-09-23: "you shouldn't level up from
# spamming, there should be regulation for everything"): each message is worth 1, drops to 0.2 after ten
# in a day, and the whole chat side is capped at CHAT_DAILY_XP_CAP so even a long chatty day cannot
# out-earn a real contribution.
XP_PER_MESSAGE = 1.0
DIMINISHING_AFTER = 10  # messages per day that count at full value
DIMINISHED_VALUE = 0.2
SHORT_MESSAGE_CHARS = 12
SHORT_MESSAGE_VALUE = 0.5
BURST_MESSAGES_PER_MINUTE = 20  # beyond this in one minute, a message scores nothing (raid guard)
CHAT_DAILY_XP_CAP = 12.0  # the most chat alone can ever pay in one day
BONUS_DAILY_CAP = 80.0  # and the most contributions can pay in one day (three event ideas' worth)

# Channel weights. Channel ids are the Chaos Redux channels (see the chaosx-discord-facts reference).
CHANNEL_WEIGHTS: dict[int, float] = {
    1396551514469699765: 0.0,  # #🤖┃bot-spam
    1525485352742031400: 0.0,  # #🗑️┃off-topic
    1490285093955309850: 1.25,  # #⚠️┃issues
    1395465288698298479: 1.25,  # #🤔┃general-suggestions
    1515282653736079381: 1.25,  # #🔎┃testers-hub
}
DEFAULT_CHANNEL_WEIGHT = 1.0

# Contributions are what actually move you (Hoops 2026-09-23: "for more meaningful contributions, like
# producing a good event idea, playtesting, etc should also all grant more points"). One accepted idea is
# worth more than three weeks of a full chat cap, so rank tracks contribution, not message count.
BONUS_XP: dict[str, float] = {
    "playtest_report": 40.0,  # a real observation from a playtest
    "event_idea": 40.0,  # an idea captured into Events/Event Specs
    "suggestion": 40.0,  # a community suggestion captured for review
    "bug_report": 25.0,  # a formatted issue that reached GitHub
    "docs_contribution": 15.0,
}

# Perks (Hoops 2026-09-23: "the higher tier you are, the more perks you get. Like your ideas take higher
# priority"). Each tier inherits everything below it. Only perks the bot can actually honour are listed -
# no promises about things no code implements.
PERKS: dict[str, tuple[str, ...]] = {
    "Calm World": (
        "your name carries the Calm World colour",
        "a written congratulation from ChaosX when you reach a new tier",
    ),
    "Gathering Storm": (
        "your tier emoji shows next to your name on the leaderboard",
    ),
    "Rising Chaos": (
        "event ideas and suggestions you post are flagged priority for review",
    ),
    "Chaos Tier": (
        "you are named in the weekly community round-up when you contribute",
    ),
    "Total Chaos": (
        "your ideas go to the top of the captured list",
    ),
    "World Collapse": (
        "a permanent place at the top of the tier panel while you stay active",
    ),
}

PERK_KEYS: dict[str, set[str]] = {
    "Calm World": {"color", "tier_up_post"},
    "Gathering Storm": {"color", "tier_up_post", "panel_emoji"},
    "Rising Chaos": {"color", "tier_up_post", "panel_emoji", "idea_priority"},
    "Chaos Tier": {"color", "tier_up_post", "panel_emoji", "idea_priority", "digest_shoutout"},
    "Total Chaos": {"color", "tier_up_post", "panel_emoji", "idea_priority", "digest_shoutout", "idea_top"},
    "World Collapse": {
        "color",
        "tier_up_post",
        "panel_emoji",
        "idea_priority",
        "digest_shoutout",
        "idea_top",
        "panel_pinned",
    },
}

# Everything below a member's tier, in ladder order, so `My tier` reads as a running list of what they
# have collected (Hoops: "perks should have more a bit" - each tier adds one, nothing is taken away).
def cumulative_perks(tier: str) -> tuple[str, ...]:
    collected: list[str] = []
    for name, _threshold in TIERS:
        collected.extend(PERKS.get(name, ()))
        if name == tier:
            break
    return tuple(collected)


def perks_for_tier(tier: str) -> tuple[str, ...]:
    """What this tier gets, newest perk last."""
    return PERKS.get(tier, ())


def has_perk(tier: str, key: str) -> bool:
    return key in PERK_KEYS.get(tier, set())


def tier_emoji(tier: str) -> str:
    return TIER_EMOJI.get(tier, "🎲")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TierProgress:
    tier: str
    xp: float
    next_tier: str | None
    xp_into_tier: float
    xp_to_next: float | None

    @property
    def label(self) -> str:
        """e.g. "Rising Chaos" or "Rising Chaos - 120/200 to Chaos Tier"."""
        if self.next_tier is None or self.xp_to_next is None:
            return self.tier
        return f"{self.tier} - {int(self.xp_into_tier)}/{int(self.xp_to_next)} to {self.next_tier}"


def tier_index(xp: float) -> int:
    """Index of the tier the given XP sits in. Negative XP counts as the lowest tier."""
    value = max(0.0, float(xp or 0))
    index = 0
    for position, (_name, threshold) in enumerate(TIERS):
        if value >= threshold:
            index = position
        else:
            break
    return index


def tier_for_xp(xp: float) -> str:
    return TIERS[tier_index(xp)][0]


def tier_threshold(tier: str) -> int | None:
    for name, threshold in TIERS:
        if name == tier:
            return threshold
    return None


def tier_progress(xp: float) -> TierProgress:
    value = max(0.0, float(xp or 0))
    index = tier_index(value)
    name, threshold = TIERS[index]
    if index + 1 >= len(TIERS):
        return TierProgress(name, value, None, value - threshold, None)
    next_name, next_threshold = TIERS[index + 1]
    span = next_threshold - threshold
    return TierProgress(name, value, next_name, value - threshold, float(span))


def meets_tier(xp: float, tier: str = DEFAULT_ELIGIBLE_TIER) -> bool:
    threshold = tier_threshold(tier)
    if threshold is None:
        return False
    return max(0.0, float(xp or 0)) >= threshold


def channel_weight(channel_id: int | None) -> float:
    if channel_id is None:
        return DEFAULT_CHANNEL_WEIGHT
    return CHANNEL_WEIGHTS.get(int(channel_id), DEFAULT_CHANNEL_WEIGHT)


def message_xp(content: str, *, channel_id: int | None, index_in_day: int) -> float:
    """XP for one message. `index_in_day` is its 0-based position among that member's day's messages."""
    weight = channel_weight(channel_id)
    if weight <= 0:
        return 0.0
    if index_in_day >= DIMINISHING_AFTER:
        base = DIMINISHED_VALUE
    else:
        base = XP_PER_MESSAGE
    text = (content or "").strip()
    if len(text) < SHORT_MESSAGE_CHARS:
        base *= SHORT_MESSAGE_VALUE
    return round(base * weight, 3)


def day_xp(messages: Sequence[tuple[str, int | None]]) -> tuple[int, float]:
    """(message_count, xp) for one member's day, in order, with the burst guard applied."""
    count = 0
    total = 0.0
    for position, (content, channel_id) in enumerate(messages):
        count += 1
        # A runaway burst (spam, or a raid) scores nothing beyond the guard.
        if position >= BURST_MESSAGES_PER_MINUTE:
            continue
        total += message_xp(content, channel_id=channel_id, index_in_day=position)
    # Hard daily ceiling on chat XP: quantity alone can never rank someone up.
    return count, round(min(total, CHAT_DAILY_XP_CAP), 3)


def parse_day(timestamp: str) -> str:
    """UTC day key for an archive timestamp; unparseable values sort under an empty-day fallback."""
    text = (timestamp or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return text[:10]


def rollup_days(rows: Iterable[tuple[int, str, int | None, str]]) -> list[tuple[int, str, int, float]]:
    """Group archive rows into (user_id, day, messages, xp).

    Rows are `(user_id, created_at, channel_id, content)` and are grouped per member per day, preserving
    order so the diminishing-returns rule is applied to the right messages.
    """
    grouped: dict[tuple[int, str], list[tuple[str, int | None]]] = {}
    for user_id, created_at, channel_id, content in rows:
        key = (int(user_id), parse_day(created_at))
        grouped.setdefault(key, []).append((content or "", channel_id))
    out: list[tuple[int, str, int, float]] = []
    for (user_id, day), messages in grouped.items():
        count, xp = day_xp(messages)
        out.append((user_id, day, count, xp))
    out.sort(key=lambda row: (row[1], row[0]))
    return out


def banter_eligible(
    *,
    xp: float,
    seen_in_channel_days_ago: int | None,
    lookback_days: int = 14,
    opted_out: bool = False,
    excluded: bool = False,
    is_bot: bool = False,
    tier: str = DEFAULT_ELIGIBLE_TIER,
) -> bool:
    """Whether a member may be targeted by idle banter.

    Every clause is a hard requirement: high chaos tier, recently active in the banter channel, not a bot,
    not excluded (Holly and Hoops are excluded in config), and not opted out.
    """
    if is_bot or excluded or opted_out:
        return False
    if seen_in_channel_days_ago is None or seen_in_channel_days_ago > lookback_days:
        return False
    return meets_tier(xp, tier)


def select_banter_candidates(
    *,
    tiers: dict[int, float],
    seen: dict[int, str],
    opted_out: set[int],
    excluded: set[int],
    bots: set[int],
    now: datetime | None = None,
    lookback_days: int = 14,
    tier: str = DEFAULT_ELIGIBLE_TIER,
) -> list[int]:
    """Member ids that idle banter may target right now, highest chaos first.

    `tiers` is user_id -> xp, `seen` is user_id -> newest message timestamp in the banter channel. An
    empty result is the correct answer while nobody has climbed to the tier floor — the bot then has no
    one to ping and must stay quiet rather than ping the channel.
    """
    moment = now or datetime.now(timezone.utc)
    picked: list[tuple[float, int]] = []
    for user_id, xp in tiers.items():
        stamp = seen.get(int(user_id))
        if stamp is None:
            continue
        try:
            seen_at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=timezone.utc)
        days_ago = max(0, (moment - seen_at).days)
        if banter_eligible(
            xp=float(xp or 0),
            seen_in_channel_days_ago=days_ago,
            lookback_days=lookback_days,
            opted_out=int(user_id) in opted_out,
            excluded=int(user_id) in excluded,
            is_bot=int(user_id) in bots,
            tier=tier,
        ):
            picked.append((float(xp or 0), int(user_id)))
    picked.sort(key=lambda item: (-item[0], item[1]))
    return [user_id for _xp, user_id in picked]


def standings_line(rank: int, name: str, progress: TierProgress, *, messages: int, days: int) -> str:
    return f"{rank}. {name} - {progress.label} ({int(progress.xp)} chaos, {messages} messages/{days} days)"


def tier_rollup(rows: Iterable[tuple[int, float]]) -> list[tuple[int, float, str]]:
    """(user_id, xp, tier) rows for the totals table."""
    return [(int(user_id), round(float(xp or 0), 3), tier_for_xp(xp)) for user_id, xp in rows]


def next_day_window(now: datetime | None = None) -> tuple[str, str]:
    moment = now or datetime.now(timezone.utc)
    return moment.date().isoformat(), (moment + timedelta(days=1)).date().isoformat()


def window_start(days: int, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return (moment - timedelta(days=max(1, days))).date().isoformat()


def as_dict(progress: TierProgress) -> dict[str, Any]:
    return {
        "tier": progress.tier,
        "xp": progress.xp,
        "next_tier": progress.next_tier,
        "label": progress.label,
        "tier_index": tier_index(progress.xp),
        "generated_at": _utcnow_iso(),
    }
