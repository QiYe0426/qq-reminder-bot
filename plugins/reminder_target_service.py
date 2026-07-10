from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from plugins.reminder_service import ReminderTarget


TARGET_ACTION_PREFIXES = ("提醒", "叫", "让", "喊", "通知", "告诉")
TARGET_PRONOUNS = ("那个人", "这个人", "她", "他", "它", "ta")
CONTENT_FILLER_PREFIXES = ("一下", "去", "要", "做")
SELF_WORDS = {"我", "自己", "本人", "我自己"}
TARGET_AUTHORIZATION_TTL = timedelta(minutes=5)

SOURCE_MENTION = "mention"
SOURCE_EXACT_MEMBER_MATCH = "exact_member_match"
SOURCE_FUZZY_MEMBER_MATCH = "fuzzy_member_match"
SOURCE_CONFIRMED_CANDIDATE = "confirmed_candidate"
SOURCE_SELF = "self"
AUTHORIZED_TARGET_SOURCES = {
    SOURCE_MENTION,
    SOURCE_EXACT_MEMBER_MATCH,
    SOURCE_CONFIRMED_CANDIDATE,
    SOURCE_SELF,
}
DIRECT_MEMBER_AUTHORIZATION_SOURCES = {SOURCE_MENTION, SOURCE_EXACT_MEMBER_MATCH}


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


@dataclass(frozen=True)
class VerifiedGroupMembers:
    group_id: str
    members: tuple[GroupMember, ...]


@dataclass(frozen=True)
class VerifiedReminderTargetCandidate:
    group_id: str
    creator_user_id: str
    target: ReminderTarget
    source: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthorizedReminderTarget:
    group_id: str
    creator_user_id: str
    target: ReminderTarget
    source: str
    expires_at: datetime


@dataclass(frozen=True)
class TargetResolution:
    content: str
    authorized_target: AuthorizedReminderTarget | None = None
    candidate: VerifiedReminderTargetCandidate | None = None
    candidates: tuple[ReminderTarget, ...] = ()
    error: str = ""

    @property
    def needs_confirmation(self) -> bool:
        return self.candidate is not None or bool(self.candidates)


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
    if not user_id.isdigit():
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


def verified_group_members(group_id: str | int, payloads: object) -> VerifiedGroupMembers:
    members: list[GroupMember] = []
    if isinstance(payloads, list):
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            member = group_member_from_payload(payload)
            if member is not None:
                members.append(member)
    return VerifiedGroupMembers(group_id=str(group_id), members=tuple(members))


def member_by_user_id(directory: VerifiedGroupMembers, user_id: str | int) -> GroupMember | None:
    normalized_user_id = clean_text(user_id)
    for member in directory.members:
        if member.user_id == normalized_user_id:
            return member
    return None


def authorized_target_is_valid(
    authorized: AuthorizedReminderTarget,
    *,
    group_id: str | int,
    creator_user_id: str | int,
    now: datetime | None = None,
) -> bool:
    current_time = now or datetime.now()
    return (
        authorized.group_id == str(group_id)
        and authorized.creator_user_id == str(creator_user_id)
        and bool(authorized.target.user_id)
        and authorized.source in AUTHORIZED_TARGET_SOURCES
        and authorized.expires_at > current_time
    )


def authorize_verified_member(
    directory: VerifiedGroupMembers,
    *,
    creator_user_id: str | int,
    target_user_id: str | int,
    source: str,
    now: datetime | None = None,
    ttl: timedelta = TARGET_AUTHORIZATION_TTL,
) -> AuthorizedReminderTarget | None:
    if source not in DIRECT_MEMBER_AUTHORIZATION_SOURCES:
        return None
    member = member_by_user_id(directory, target_user_id)
    if member is None:
        return None
    return AuthorizedReminderTarget(
        group_id=directory.group_id,
        creator_user_id=str(creator_user_id),
        target=ReminderTarget(user_id=member.user_id, display_name=member.display_name),
        source=source,
        expires_at=(now or datetime.now()) + ttl,
    )


def authorize_self_target(
    *,
    group_id: str | int,
    creator_user_id: str | int,
    display_name: str = "",
    now: datetime | None = None,
    ttl: timedelta = TARGET_AUTHORIZATION_TTL,
) -> AuthorizedReminderTarget:
    return AuthorizedReminderTarget(
        group_id=str(group_id),
        creator_user_id=str(creator_user_id),
        target=ReminderTarget(user_id=str(creator_user_id), display_name=display_name),
        source=SOURCE_SELF,
        expires_at=(now or datetime.now()) + ttl,
    )


def verified_candidate(
    directory: VerifiedGroupMembers,
    *,
    creator_user_id: str | int,
    target_user_id: str | int,
    source: str,
    now: datetime | None = None,
    ttl: timedelta = TARGET_AUTHORIZATION_TTL,
) -> VerifiedReminderTargetCandidate | None:
    member = member_by_user_id(directory, target_user_id)
    if member is None:
        return None
    return VerifiedReminderTargetCandidate(
        group_id=directory.group_id,
        creator_user_id=str(creator_user_id),
        target=ReminderTarget(user_id=member.user_id, display_name=member.display_name),
        source=source,
        expires_at=(now or datetime.now()) + ttl,
    )


def authorize_confirmed_candidate(
    candidate: VerifiedReminderTargetCandidate,
    *,
    group_id: str | int,
    creator_user_id: str | int,
    target_user_id: str | int,
    now: datetime | None = None,
    ttl: timedelta = TARGET_AUTHORIZATION_TTL,
) -> AuthorizedReminderTarget | None:
    if (
        candidate.group_id != str(group_id)
        or candidate.creator_user_id != str(creator_user_id)
        or candidate.target.user_id != str(target_user_id)
        or candidate.expires_at <= (now or datetime.now())
    ):
        return None
    return AuthorizedReminderTarget(
        group_id=candidate.group_id,
        creator_user_id=candidate.creator_user_id,
        target=candidate.target,
        source=SOURCE_CONFIRMED_CANDIDATE,
        expires_at=(now or datetime.now()) + ttl,
    )


def renew_authorized_target(
    authorized: AuthorizedReminderTarget,
    *,
    ttl: timedelta,
    now: datetime | None = None,
) -> AuthorizedReminderTarget:
    return AuthorizedReminderTarget(
        group_id=authorized.group_id,
        creator_user_id=authorized.creator_user_id,
        target=authorized.target,
        source=authorized.source,
        expires_at=(now or datetime.now()) + ttl,
    )


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


def member_target_matches(
    content: str,
    members: tuple[GroupMember, ...] | list[GroupMember],
    *,
    allow_fuzzy_without_action: bool = False,
) -> list[TargetMatch]:
    candidate_text, had_action_prefix = strip_target_action_prefix(content)
    compact_candidate_text = compact_text(candidate_text)
    if not compact_candidate_text or compact_candidate_text in SELF_WORDS or content.startswith(("提醒我", "叫我", "让我")):
        return []

    exact_matches: list[tuple[int, GroupMember, str, bool]] = []
    fuzzy_matches: list[tuple[int, GroupMember, str, bool]] = []
    for member in members:
        for alias in member.aliases:
            compact_alias = compact_text(alias)
            if len(compact_alias) < 2:
                continue
            if compact_candidate_text.startswith(compact_alias):
                exact_matches.append((1000 + len(compact_alias), member, candidate_text[: len(alias)], True))
            elif (had_action_prefix or allow_fuzzy_without_action) and len(compact_candidate_text) >= 2 and compact_alias.startswith(compact_candidate_text[:2]):
                fuzzy_matches.append((100 + len(compact_alias), member, candidate_text[:2], False))
    return unique_member_candidates(exact_matches or fuzzy_matches, candidate_text)


def resolve_verified_target_in_content(
    content: str,
    directory: VerifiedGroupMembers,
    *,
    creator_user_id: str | int,
    allow_fuzzy_without_action: bool = False,
    now: datetime | None = None,
) -> TargetResolution | None:
    matches = member_target_matches(
        content,
        directory.members,
        allow_fuzzy_without_action=allow_fuzzy_without_action,
    )
    if not matches:
        return None
    if len(matches) > 1:
        return TargetResolution(
            content=matches[0].content,
            candidates=tuple(match.target for match in matches),
            error="找到了多个可能的群成员，请直接 @一个人确认提醒对象。",
        )

    match = matches[0]
    if match.needs_confirmation:
        candidate = verified_candidate(
            directory,
            creator_user_id=creator_user_id,
            target_user_id=match.target.user_id,
            source=SOURCE_FUZZY_MEMBER_MATCH,
            now=now,
        )
        if candidate is None:
            return TargetResolution(content=match.content, error="提醒对象不在当前群成员列表中。")
        return TargetResolution(content=match.content, candidate=candidate)

    authorized = authorize_verified_member(
        directory,
        creator_user_id=creator_user_id,
        target_user_id=match.target.user_id,
        source=SOURCE_EXACT_MEMBER_MATCH,
        now=now,
    )
    if authorized is None:
        return TargetResolution(content=match.content, error="提醒对象不在当前群成员列表中。")
    return TargetResolution(content=match.content, authorized_target=authorized)


def resolve_mentioned_targets(
    target_user_ids: list[str],
    directory: VerifiedGroupMembers,
    *,
    creator_user_id: str | int,
    content: str,
    now: datetime | None = None,
) -> TargetResolution:
    unique_ids: list[str] = []
    for target_user_id in target_user_ids:
        normalized = clean_text(target_user_id)
        if normalized and normalized not in unique_ids:
            unique_ids.append(normalized)

    members: list[GroupMember] = []
    for target_user_id in unique_ids:
        member = member_by_user_id(directory, target_user_id)
        if member is None:
            return TargetResolution(content=content, error=f"QQ {target_user_id} 不是当前群的已验证成员。")
        members.append(member)

    if not members:
        return TargetResolution(content=content, error="没有找到有效的提醒对象。")
    if len(members) > 1:
        return TargetResolution(
            content=content,
            candidates=tuple(ReminderTarget(member.user_id, member.display_name) for member in members),
            error="一次只能提醒一个人。请只 @一个群成员后重试。",
        )

    authorized = authorize_verified_member(
        directory,
        creator_user_id=creator_user_id,
        target_user_id=members[0].user_id,
        source=SOURCE_MENTION,
        now=now,
    )
    return TargetResolution(content=content, authorized_target=authorized)


def find_target_in_content(
    content: str,
    members: list[GroupMember],
    *,
    allow_fuzzy_without_action: bool = False,
) -> TargetMatch | None:
    candidates = member_target_matches(
        content,
        members,
        allow_fuzzy_without_action=allow_fuzzy_without_action,
    )
    if not candidates:
        return None
    if len(candidates) == 1 and not candidates[0].needs_confirmation:
        return candidates[0]
    first = candidates[0]
    return TargetMatch(target=first.target, content=first.content, needs_confirmation=True)
