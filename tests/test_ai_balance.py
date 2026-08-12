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
    QWEN_MODULE_IMAGE_VISION,
    QWEN_MODULE_KEYWORD_SCAN,
    QwenProbeConfig,
    QwenProbeResult,
    balance_command_text,
    build_ai_api_status_report,
    build_balance_url,
    build_qwen_probe_url,
    discover_qwen_probe_configs,
    fetch_deepseek_balance,
    format_qwen_status,
    format_deepseek_status,
    format_deepseek_balance,
    probe_qwen_status,
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
        self.request_json: dict[str, Any] = {}

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

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> FakeResponse:
        self.request_url = url
        self.request_headers = headers
        self.request_json = json
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

    result = asyncio.run(
        balance_command_text(
            private_event(10001),
            fetcher=fetcher,
            qwen_configs=[],
        )
    )
    assert result is not None
    assert result.startswith("AI API 状态")
    assert "总余额 ¥12.34" in result
    assert "负责模块：AI 对话、Agent 工具调度" in result


def test_balance_command_denies_non_admin_before_querying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_ADMIN_USER_IDS", "10001")
    called = False

    async def fetcher() -> dict[str, Any]:
        nonlocal called
        called = True
        return VALID_PAYLOAD

    result = asyncio.run(
        balance_command_text(
            private_event(10002),
            fetcher=fetcher,
            qwen_configs=[],
        )
    )
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

    result = asyncio.run(
        balance_command_text(
            group_event(10001),
            fetcher=fetcher,
            qwen_configs=[],
        )
    )
    assert result is None
    assert not called


def test_balance_command_returns_safe_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_ADMIN_USER_IDS", "10001")

    async def fetcher() -> dict[str, Any]:
        raise DeepSeekBalanceError("DeepSeek 服务暂时不可用，请稍后重试。")

    result = asyncio.run(
        balance_command_text(
            private_event(10001),
            fetcher=fetcher,
            qwen_configs=[],
        )
    )
    assert result is not None
    assert "DeepSeek\n" in result
    assert "状态：查询失败" in result
    assert "DeepSeek 服务暂时不可用，请稍后重试。" in result


def test_balance_commands_and_plugin_are_registered_and_documented() -> None:
    assert BALANCE_COMMANDS == ("查询 AI 余额", "查询余额", "AI余额")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert '"plugins.ai_balance"' in pyproject
    assert all(command in readme for command in BALANCE_COMMANDS)
    assert "仅管理员私聊可用" in readme


def test_discover_qwen_configs_uses_image_fallbacks() -> None:
    configs = discover_qwen_probe_configs(
        {
            "IMAGE_VISION_API_KEY": "image-key",
            "IMAGE_VISION_BASE_URL": "https://dashscope.example/compatible-mode/v1",
            "IMAGE_VISION_MODEL": "qwen-vl-test",
        }
    )

    assert configs == [
        QwenProbeConfig(
            modules=(QWEN_MODULE_IMAGE_VISION, QWEN_MODULE_KEYWORD_SCAN),
            api_key="image-key",
            base_url="https://dashscope.example/compatible-mode/v1",
            model="qwen-vl-test",
        )
    ]


def test_discover_qwen_configs_falls_back_to_openai_compatible_settings() -> None:
    configs = discover_qwen_probe_configs(
        {
            "OPENAI_API_KEY": "fallback-key",
            "OPENAI_BASE_URL": "https://gateway.example/v1",
        }
    )

    assert len(configs) == 1
    assert configs[0].api_key == "fallback-key"
    assert configs[0].base_url == "https://gateway.example/v1"
    assert configs[0].model == "qwen3-vl-flash"
    assert configs[0].modules == (QWEN_MODULE_IMAGE_VISION, QWEN_MODULE_KEYWORD_SCAN)


def test_discover_qwen_configs_keeps_distinct_keyword_profile() -> None:
    configs = discover_qwen_probe_configs(
        {
            "IMAGE_VISION_API_KEY": "image-key",
            "IMAGE_VISION_BASE_URL": "https://image.example/v1",
            "IMAGE_VISION_MODEL": "qwen-image",
            "KEYWORD_RETORT_IMAGE_SCAN_API_KEY": "keyword-key",
            "KEYWORD_RETORT_IMAGE_SCAN_BASE_URL": "https://keyword.example/v1",
            "KEYWORD_RETORT_IMAGE_SCAN_MODEL": "qwen-keyword",
        }
    )

    assert configs == [
        QwenProbeConfig(
            modules=(QWEN_MODULE_IMAGE_VISION,),
            api_key="image-key",
            base_url="https://image.example/v1",
            model="qwen-image",
        ),
        QwenProbeConfig(
            modules=(QWEN_MODULE_KEYWORD_SCAN,),
            api_key="keyword-key",
            base_url="https://keyword.example/v1",
            model="qwen-keyword",
        ),
    ]


def test_discover_qwen_configs_omits_unconfigured_profiles() -> None:
    assert discover_qwen_probe_configs({}) == []


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        ),
        ("https://gateway.example/v1/", "https://gateway.example/v1/chat/completions"),
        (
            "https://gateway.example/v1/chat/completions",
            "https://gateway.example/v1/chat/completions",
        ),
    ],
)
def test_build_qwen_probe_url(base_url: str, expected: str) -> None:
    assert build_qwen_probe_url(base_url) == expected


QWEN_CONFIG = QwenProbeConfig(
    modules=(QWEN_MODULE_IMAGE_VISION, QWEN_MODULE_KEYWORD_SCAN),
    api_key="qwen-test-secret",
    base_url="https://dashscope.example/compatible-mode/v1",
    model="qwen3-vl-flash",
)


def test_probe_qwen_status_sends_one_token_request() -> None:
    created: list[FakeSession] = []

    def session_factory(**kwargs: Any) -> FakeSession:
        session = FakeSession(response=FakeResponse(payload={"choices": []}), **kwargs)
        created.append(session)
        return session

    result = asyncio.run(
        probe_qwen_status(QWEN_CONFIG, session_factory=session_factory)
    )

    assert result == QwenProbeResult(status="可用", detail="模型调用正常")
    assert created[0].request_url.endswith("/compatible-mode/v1/chat/completions")
    assert created[0].request_headers == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer qwen-test-secret",
    }
    assert created[0].request_json == {
        "model": "qwen3-vl-flash",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    assert created[0].init_kwargs["timeout"].total == 10


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (
            400,
            {"error": {"code": "Arrearage", "message": "raw-private-arrearage"}},
            QwenProbeResult(status="欠费", detail="账户欠费，请前往阿里云充值"),
        ),
        (
            401,
            {"error": {"code": "InvalidApiKey", "message": "raw-private-key"}},
            QwenProbeResult(status="Key 无效", detail="API Key 无效或已失效"),
        ),
        (
            403,
            {"error": {"code": "AccessDenied", "message": "raw-private-denied"}},
            QwenProbeResult(status="Key 无效", detail="API Key 无权限或已失效"),
        ),
        (
            429,
            {"error": {"code": "Throttling", "message": "raw-private-rate"}},
            QwenProbeResult(status="限流", detail="请求过于频繁，请稍后重试"),
        ),
        (
            503,
            {"error": {"code": "InternalError", "message": "raw-private-server"}},
            QwenProbeResult(status="服务异常", detail="阿里云百炼服务暂时不可用"),
        ),
    ],
)
def test_probe_qwen_status_classifies_provider_errors_without_leaking(
    status: int,
    payload: object,
    expected: QwenProbeResult,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def session_factory(**kwargs: Any) -> FakeSession:
        return FakeSession(response=FakeResponse(status=status, payload=payload), **kwargs)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            probe_qwen_status(QWEN_CONFIG, session_factory=session_factory)
        )

    assert result == expected
    combined = result.detail + caplog.text
    assert QWEN_CONFIG.api_key not in combined
    assert "raw-private" not in combined


def test_probe_qwen_status_classifies_arrearage_code_even_with_http_200() -> None:
    def session_factory(**kwargs: Any) -> FakeSession:
        return FakeSession(
            response=FakeResponse(
                payload={"code": "Arrearage", "message": "raw-private-arrearage"}
            ),
            **kwargs,
        )

    assert asyncio.run(
        probe_qwen_status(QWEN_CONFIG, session_factory=session_factory)
    ) == QwenProbeResult(status="欠费", detail="账户欠费，请前往阿里云充值")


def test_probe_qwen_status_maps_timeout_without_leaking() -> None:
    def session_factory(**kwargs: Any) -> FakeSession:
        return FakeSession(request_error=asyncio.TimeoutError("raw-private-timeout"), **kwargs)

    result = asyncio.run(
        probe_qwen_status(QWEN_CONFIG, session_factory=session_factory)
    )
    assert result == QwenProbeResult(status="服务异常", detail="Qwen 状态查询超时")


def test_probe_qwen_status_maps_network_failure_without_leaking() -> None:
    def session_factory(**kwargs: Any) -> FakeSession:
        return FakeSession(request_error=OSError("raw-private-network"), **kwargs)

    result = asyncio.run(
        probe_qwen_status(QWEN_CONFIG, session_factory=session_factory)
    )
    assert result == QwenProbeResult(status="服务异常", detail="无法连接阿里云百炼服务")


def test_probe_qwen_status_maps_invalid_json_safely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def session_factory(**kwargs: Any) -> FakeSession:
        return FakeSession(
            response=FakeResponse(json_error=ValueError("raw-private-json")),
            **kwargs,
        )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            probe_qwen_status(QWEN_CONFIG, session_factory=session_factory)
        )
    assert result == QwenProbeResult(status="服务异常", detail="Qwen 返回了异常响应")
    assert "raw-private-json" not in caplog.text


def test_format_qwen_status_includes_modules_model_and_probe_cost() -> None:
    text = format_qwen_status(
        QWEN_CONFIG,
        QwenProbeResult(status="可用", detail="模型调用正常"),
    )

    assert "Qwen / 阿里云百炼" in text
    assert "负责模块：媒体图片识别、关键词回怼图片文字检测" in text
    assert "模型：qwen3-vl-flash" in text
    assert "状态：可用" in text
    assert "最多生成 1 token" in text


def test_format_deepseek_status_labels_responsible_modules() -> None:
    text = format_deepseek_status(VALID_PAYLOAD)

    assert text.startswith("DeepSeek\n")
    assert "负责模块：AI 对话、Agent 工具调度、联网决策、陪伴画像" in text
    assert "日报总结（回退配置）" in text
    assert "状态：可用" in text
    assert "CNY：总余额 ¥12.34" in text


def test_build_ai_api_status_report_combines_providers() -> None:
    async def deepseek_fetcher() -> dict[str, Any]:
        return VALID_PAYLOAD

    async def qwen_probe(config: QwenProbeConfig) -> QwenProbeResult:
        assert config == QWEN_CONFIG
        return QwenProbeResult(status="欠费", detail="账户欠费，请前往阿里云充值")

    report = asyncio.run(
        build_ai_api_status_report(
            deepseek_fetcher=deepseek_fetcher,
            qwen_configs=[QWEN_CONFIG],
            qwen_probe=qwen_probe,
        )
    )

    assert report.startswith("AI API 状态\n\nDeepSeek")
    assert report.index("DeepSeek") < report.index("Qwen / 阿里云百炼")
    assert "状态：欠费" in report
    assert "账户欠费，请前往阿里云充值" in report


def test_build_ai_api_status_report_keeps_qwen_when_deepseek_fails() -> None:
    async def deepseek_fetcher() -> dict[str, Any]:
        raise DeepSeekBalanceError("DeepSeek 查询失败安全提示")

    async def qwen_probe(config: QwenProbeConfig) -> QwenProbeResult:
        return QwenProbeResult(status="可用", detail="模型调用正常")

    report = asyncio.run(
        build_ai_api_status_report(
            deepseek_fetcher=deepseek_fetcher,
            qwen_configs=[QWEN_CONFIG],
            qwen_probe=qwen_probe,
        )
    )

    assert "状态：查询失败" in report
    assert "DeepSeek 查询失败安全提示" in report
    assert "Qwen / 阿里云百炼" in report
    assert "状态：可用" in report


def test_build_ai_api_status_report_formats_multiple_qwen_profiles() -> None:
    second = QwenProbeConfig(
        modules=(QWEN_MODULE_KEYWORD_SCAN,),
        api_key="second-secret",
        base_url="https://second.example/v1",
        model="qwen-second",
    )

    async def deepseek_fetcher() -> dict[str, Any]:
        return VALID_PAYLOAD

    async def qwen_probe(config: QwenProbeConfig) -> QwenProbeResult:
        return QwenProbeResult(status="可用", detail="模型调用正常")

    report = asyncio.run(
        build_ai_api_status_report(
            deepseek_fetcher=deepseek_fetcher,
            qwen_configs=[QWEN_CONFIG, second],
            qwen_probe=qwen_probe,
        )
    )

    assert report.count("Qwen / 阿里云百炼") == 2
    assert "模型：qwen3-vl-flash" in report
    assert "模型：qwen-second" in report
    assert "qwen-test-secret" not in report
    assert "second-secret" not in report


def test_build_ai_api_status_report_marks_qwen_unconfigured() -> None:
    async def deepseek_fetcher() -> dict[str, Any]:
        return VALID_PAYLOAD

    report = asyncio.run(
        build_ai_api_status_report(
            deepseek_fetcher=deepseek_fetcher,
            qwen_configs=[],
        )
    )

    assert "Qwen / 阿里云百炼" in report
    assert "状态：未配置" in report
    assert "未找到图片识别 API Key、服务地址或模型配置" in report


def test_readme_describes_qwen_probe_scope_and_cost() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Qwen 在线状态" in readme
    assert "最多生成 1 token" in readme
    assert "不查询阿里云人民币余额" in readme
