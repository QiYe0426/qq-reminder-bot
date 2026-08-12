from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

nonebot.init()

from plugins.ai_balance import (
    BALANCE_COMMANDS,
    DeepSeekBalanceError,
    balance_command_text,
    build_balance_url,
    fetch_deepseek_balance,
    format_deepseek_balance,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("", "https://api.deepseek.com/user/balance"),
        ("https://api.deepseek.com", "https://api.deepseek.com/user/balance"),
        ("https://api.deepseek.com/", "https://api.deepseek.com/user/balance"),
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/user/balance"),
        ("https://gateway.example/v1/", "https://gateway.example/user/balance"),
    ],
)
def test_build_balance_url_normalizes_openai_compatible_base_url(
    base_url: str,
    expected: str,
) -> None:
    assert build_balance_url(base_url) == expected


def test_format_deepseek_balance_renders_multiple_currencies() -> None:
    payload = {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "CNY",
                "total_balance": "12.3400",
                "granted_balance": "2.3400",
                "topped_up_balance": "10.0000",
            },
            {
                "currency": "USD",
                "total_balance": "1.25",
                "granted_balance": "0.25",
                "topped_up_balance": "1.00",
            },
        ],
    }

    assert format_deepseek_balance(payload) == (
        "DeepSeek API 余额\n"
        "状态：可用\n"
        "CNY：总余额 ¥12.3400（赠金 ¥2.3400，充值 ¥10.0000）\n"
        "USD：总余额 $1.25（赠金 $0.25，充值 $1.00）"
    )


def test_format_deepseek_balance_marks_unavailable_account() -> None:
    payload = {
        "is_available": False,
        "balance_infos": [
            {
                "currency": "CNY",
                "total_balance": "0.00",
                "granted_balance": "0.00",
                "topped_up_balance": "0.00",
            }
        ],
    }

    assert "状态：不可用（请检查余额或账户状态）" in format_deepseek_balance(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"is_available": "yes", "balance_infos": []},
        {"is_available": True, "balance_infos": "invalid"},
        {"is_available": True, "balance_infos": [{}]},
        {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "CNY",
                    "total_balance": 1.0,
                    "granted_balance": "0",
                    "topped_up_balance": "1",
                }
            ],
        },
    ],
)
def test_format_deepseek_balance_rejects_malformed_payload(payload: object) -> None:
    with pytest.raises(DeepSeekBalanceError, match="响应格式异常"):
        format_deepseek_balance(payload)


VALID_PAYLOAD = {
    "is_available": True,
    "balance_infos": [
        {
            "currency": "CNY",
            "total_balance": "12.34",
            "granted_balance": "2.34",
            "topped_up_balance": "10.00",
        }
    ],
}


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        payload: object = VALID_PAYLOAD,
        json_error: Exception | None = None,
    ) -> None:
        self.status = status
        self._payload = payload
        self._json_error = json_error

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeSession:
    def __init__(
        self,
        response: FakeResponse | None = None,
        request_error: Exception | None = None,
        **kwargs: Any,
    ) -> None:
        self.response = response or FakeResponse()
        self.request_error = request_error
        self.init_kwargs = kwargs
        self.request_url = ""
        self.request_headers: dict[str, str] = {}

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
        self.request_url = url
        self.request_headers = headers
        if self.request_error is not None:
            raise self.request_error
        return self.response


def test_fetch_deepseek_balance_sends_safe_authenticated_request() -> None:
    created: list[FakeSession] = []

    def session_factory(**kwargs: Any) -> FakeSession:
        session = FakeSession(**kwargs)
        created.append(session)
        return session

    result = asyncio.run(
        fetch_deepseek_balance(
            api_key="test-secret-key",
            base_url="https://api.deepseek.com/v1",
            session_factory=session_factory,
        )
    )

    assert result == VALID_PAYLOAD
    assert created[0].request_url == "https://api.deepseek.com/user/balance"
    assert created[0].request_headers == {
        "Accept": "application/json",
        "Authorization": "Bearer test-secret-key",
    }
    assert created[0].init_kwargs["timeout"].total == 10


def test_fetch_deepseek_balance_rejects_missing_api_key_without_request() -> None:
    called = False

    def session_factory(**kwargs: Any) -> FakeSession:
        nonlocal called
        called = True
        return FakeSession(**kwargs)

    with pytest.raises(DeepSeekBalanceError, match="API Key 未配置"):
        asyncio.run(fetch_deepseek_balance(api_key="", session_factory=session_factory))
    assert not called


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "API Key 无效或已失效"),
        (402, "余额不足"),
        (429, "请求过于频繁"),
        (500, "服务暂时不可用"),
    ],
)
def test_fetch_deepseek_balance_maps_http_errors_safely(
    status: int,
    message: str,
) -> None:
    def session_factory(**kwargs: Any) -> FakeSession:
        return FakeSession(response=FakeResponse(status=status), **kwargs)

    with pytest.raises(DeepSeekBalanceError, match=message):
        asyncio.run(
            fetch_deepseek_balance(
                api_key="test-secret-key",
                session_factory=session_factory,
            )
        )


def test_fetch_deepseek_balance_maps_timeout_safely() -> None:
    def session_factory(**kwargs: Any) -> FakeSession:
        return FakeSession(request_error=asyncio.TimeoutError("raw-timeout"), **kwargs)

    with pytest.raises(DeepSeekBalanceError, match="请求超时") as exc_info:
        asyncio.run(
            fetch_deepseek_balance(
                api_key="test-secret-key",
                session_factory=session_factory,
            )
        )
    assert "raw-timeout" not in str(exc_info.value)


def test_fetch_deepseek_balance_rejects_invalid_json_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_marker = "raw-response-private-marker"

    def session_factory(**kwargs: Any) -> FakeSession:
        return FakeSession(
            response=FakeResponse(json_error=ValueError(raw_marker)),
            **kwargs,
        )

    with caplog.at_level(logging.WARNING), pytest.raises(
        DeepSeekBalanceError,
        match="响应格式异常",
    ) as exc_info:
        asyncio.run(
            fetch_deepseek_balance(
                api_key="test-secret-key",
                session_factory=session_factory,
            )
        )
    combined = str(exc_info.value) + caplog.text
    assert "test-secret-key" not in combined
    assert raw_marker not in combined


def test_fetch_deepseek_balance_validates_response_structure() -> None:
    def session_factory(**kwargs: Any) -> FakeSession:
        return FakeSession(response=FakeResponse(payload={"unexpected": True}), **kwargs)

    with pytest.raises(DeepSeekBalanceError, match="响应格式异常"):
        asyncio.run(
            fetch_deepseek_balance(
                api_key="test-secret-key",
                session_factory=session_factory,
            )
        )


def private_event(user_id: int) -> PrivateMessageEvent:
    message = Message("查询 AI 余额")
    return PrivateMessageEvent(
        time=0,
        self_id=999,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=1,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender=Sender(user_id=user_id, nickname="tester"),
        to_me=True,
    )


def group_event(user_id: int) -> GroupMessageEvent:
    message = Message("查询 AI 余额")
    return GroupMessageEvent(
        time=0,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=1,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender=Sender(user_id=user_id, nickname="tester", role="member"),
        to_me=True,
        group_id=20001,
    )


def test_balance_command_allows_configured_admin_private_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_ADMIN_USER_IDS", "10001")

    async def fetcher() -> dict[str, Any]:
        return VALID_PAYLOAD

    result = asyncio.run(balance_command_text(private_event(10001), fetcher=fetcher))
    assert result is not None
    assert "总余额 ¥12.34" in result


def test_balance_command_denies_non_admin_before_querying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_ADMIN_USER_IDS", "10001")
    called = False

    async def fetcher() -> dict[str, Any]:
        nonlocal called
        called = True
        return VALID_PAYLOAD

    result = asyncio.run(balance_command_text(private_event(10002), fetcher=fetcher))
    assert result == "这个命令只有管理员可以使用。"
    assert not called


def test_balance_command_silently_ignores_group_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_ADMIN_USER_IDS", "10001")
    called = False

    async def fetcher() -> dict[str, Any]:
        nonlocal called
        called = True
        return VALID_PAYLOAD

    result = asyncio.run(balance_command_text(group_event(10001), fetcher=fetcher))
    assert result is None
    assert not called


def test_balance_command_returns_safe_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_ADMIN_USER_IDS", "10001")

    async def fetcher() -> dict[str, Any]:
        raise DeepSeekBalanceError("DeepSeek 服务暂时不可用，请稍后重试。")

    result = asyncio.run(balance_command_text(private_event(10001), fetcher=fetcher))
    assert result == "DeepSeek 服务暂时不可用，请稍后重试。"


def test_balance_commands_and_plugin_are_registered_and_documented() -> None:
    assert BALANCE_COMMANDS == ("查询 AI 余额", "查询余额", "AI余额")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert '"plugins.ai_balance"' in pyproject
    assert all(command in readme for command in BALANCE_COMMANDS)
    assert "仅管理员私聊可用" in readme
