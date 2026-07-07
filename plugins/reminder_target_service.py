from __future__ import annotations

import re
from dataclasses import dataclass

from plugins.reminder_service import ReminderTarget


TARGET_ACTION_PREFIXES = ("提醒", "叫", "让", "喊", "通知", "告诉")
TARGET_PRONOUNS = ("那个人", "这个人", "她", "他", "它", "ta")
CONTENT_FILLER_PREFIXES = ("一下", "去", "要", "做")
SELF_WORDS = {"我", "自己", "本人", "我自己"}


@dataclass(frozen=True)
class GroupMember:
    user_id: str
    display_name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class TargetMatch:
    target: ReminderTarget
    content: str
    needs_confirmation: bool = False


def clean_text(text: object) -> str:
    return " ".join(str(text or "").split()).strip()


def compact_text(text: str) -> str:
    return re.sub(r"[\s，,。；;：:！!？?、~～]+", "", text or "").casefold()


def strip_target_action_prefix(content: str) -> tuple[str, bool]:
    normalized = content.strip().strip("，,。；;：:")
    for prefix in TARGET_ACTION_PREFIXES:
        if normalized.startswith(prefix):
            stripped = normalized[len(prefix) :].strip().strip("，,。；;：:")
            return stripped or normalized, bool(stripped)
    return normalized, False


def strip_content_filler_prefixes(content: str) -> str:
    normalized = content.strip().strip("，,。；;：:")
    changed = True
    while changed:
        changed = False
        for prefix in CONTENT_FILLER_PREFIXES:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :].strip().strip("，,。；;：:")
                changed = True
                break
    return normalized


def strip_target_pronoun(content: str) -> tuple[str, bool]:
    """Remove a leading reminder target pronoun from content.

    Examples:
    - 提醒她喝水 -> 喝水
    - 叫 ta 一下喝水 -> 喝水
    - 那个人去睡觉 -> 睡觉
    """

    candidate_text, _ = strip_target_action_prefix(content)
    candidate_text = strip_content_filler_prefixes(candidate_text)
    compact_candidate = compact_text(candidate_text)
    if not compact_candidate:
        return candidate_text, False

    for pronoun in sorted(TARGET_PRONOUNS, key=len, reverse=True):
        compact_pronoun = compact_text(pronoun)
        if not compact_candidate.startswith(compact_pronoun):
            continue

        if pronoun.isascii():
            remainder = candidate_text[len(pronoun) :]
        else:
            remainder = candidate_text[len(pronoun) :]
        remainder = strip_content_filler_prefixes(remainder)
        return remainder or candidate_text, bool(remainder)

    return candidate_text, False


def group_member_from_payload(payload: dict[str, object]) -> GroupMember | None:
    user_id = clean_text(payload.get("user_id"))
    if not user_id:
        return None
    card = clean_text(payload.get("card"))
    nickname = clean_text(payload.get("nickname"))
    display_name = card or nickname or user_id
    raw_aliases = [card, nickname, display_name, user_id]
    aliases: list[str] = []
    for alias in raw_aliases:
        alias = clean_text(alias)
        if alias and alias not in aliases:
            aliases.append(alias)
    return GroupMember(user_id=user_id, display_name=display_name, aliases=tuple(aliases))


def content_without_prefix(prefix: str, content: str) -> str:
    stripped = content[len(prefix) :].strip().strip("，,。；;：:")
    return strip_content_filler_prefixes(stripped)


def unique_member_candidates(matches: list[tuple[int, GroupMember, str, bool]], content: str) -> list[TargetMatch]:
    best_by_user: dict[str, tuple[int, GroupMember, str, bool]] = {}
    for score, member, alias, is_exact in matches:
        current = best_by_user.get(member.user_id)
        if current is None or score > current[0]:
            best_by_user[member.user_id] = (score, member, alias, is_exact)

    sorted_matches = sorted(best_by_user.values(), key=lambda item: item[0], reverse=True)
    result: list[TargetMatch] = []
    for score, member, alias, is_exact in sorted_matches:
        reminder_content = content_without_prefix(alias, content) if is_exact else content_without_prefix(content[:2], content)
        if not reminder_content:
            continue
        result.append(
            TargetMatch(
                target=ReminderTarget(user_id=member.user_id, display_name=member.display_name),
                content=reminder_content,
                needs_confirmation=not is_exact,
            )
        )
    return result


def find_target_in_content(content: str, members: list[GroupMember]) -> TargetMatch | None:
    candidate_text, had_action_prefix = strip_target_action_prefix(content)
    compact_candidate_text = compact_text(candidate_text)
    if not compact_candidate_text or compact_candidate_text in SELF_WORDS or content.startswith(("提醒我", "叫我", "让我")):
        return None

    exact_matches: list[tuple[int, GroupMember, str, bool]] = []
    fuzzy_matches: list[tuple[int, GroupMember, str, bool]] = []
    for member in members:
        for alias in member.aliases:
            compact_alias = compact_text(alias)
            if len(compact_alias) < 2:
                continue
            if compact_candidate_text.startswith(compact_alias):
                exact_matches.append((1000 + len(compact_alias), member, candidate_text[: len(alias)], True))
            elif had_action_prefix and len(compact_candidate_text) >= 2 and compact_alias.startswith(compact_candidate_text[:2]):
                fuzzy_matches.append((100 + len(compact_alias), member, candidate_text[:2], False))

    candidates = unique_member_candidates(exact_matches or fuzzy_matches, candidate_text)
    if not candidates:
        return None
    if len(candidates) == 1 and not candidates[0].needs_confirmation:
        return candidates[0]
    first = candidates[0]
    return TargetMatch(target=first.target, content=first.content, needs_confirmation=True)
