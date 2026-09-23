from __future__ import annotations

import httpx
import pytest

from multiplier_core import (
    Sub2APIGroupBucket,
    Sub2APIGroupHealth,
    Sub2APIGroupRate,
    Sub2APIInstanceConfig,
    build_auth_headers,
    build_groups_url,
    build_health_overview,
    build_monitor_url,
    build_report,
    fetch_group_health,
    fetch_groups,
    format_report,
    split_message,
)


def test_build_groups_url_validates_domain_and_query():
    assert build_groups_url("https://example.com/", False) == (
        "https://example.com/api/v1/admin/groups/all?include_inactive=false"
    )
    assert build_groups_url("https://example.com", True).endswith(
        "include_inactive=true"
    )

    with pytest.raises(Exception, match="域名无效"):
        build_groups_url("example.com")


def test_build_monitor_url_uses_platform_group_and_validates_range():
    assert build_monitor_url("https://example.com/", "24h") == (
        "https://example.com/api/v1/admin/channel-monitor-v2/matrix?"
        "range=24h&group_by=platform_group"
    )

    with pytest.raises(Exception, match="时间范围无效"):
        build_monitor_url("https://example.com", "2h")


@pytest.mark.asyncio
async def test_fetch_groups_sends_admin_api_key_and_parses_models():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 1,
                        "name": "便宜组",
                        "platform": "openai",
                        "status": "active",
                        "rate_multiplier": "0.5",
                        "model_pricing": [
                            {"model": "gpt-4o", "input_price": 1},
                            {"model": "gpt-4o-mini", "input_price": 0.1},
                        ],
                    }
                ]
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        groups = await fetch_groups(
            Sub2APIInstanceConfig("test", "https://example.com", "secret-key"),
            client=client,
        )
    finally:
        await client.aclose()

    assert captured["key"] == "secret-key"
    assert captured["url"].endswith("include_inactive=false")
    assert groups[0].rate_multiplier == 0.5
    assert groups[0].model_names == ("gpt-4o", "gpt-4o-mini")


@pytest.mark.asyncio
async def test_fetch_groups_filters_inactive_groups():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 1,
                        "name": "启用组",
                        "platform": "openai",
                        "status": "active",
                        "rate_multiplier": 1,
                    },
                    {
                        "id": 2,
                        "name": "停用组",
                        "platform": "openai",
                        "status": "inactive",
                        "rate_multiplier": 0.1,
                    },
                ]
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        groups = await fetch_groups(
            Sub2APIInstanceConfig("test", "https://example.com", "secret-key"),
            client=client,
        )
    finally:
        await client.aclose()

    assert [group.name for group in groups] == ["启用组"]


@pytest.mark.asyncio
async def test_fetch_group_health_parses_cache_rate_and_status():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={
                "data": {
                    "items": [
                        {
                            "group_id": 1,
                            "group_name": "便宜组",
                            "platform": "openai",
                            "metrics": {
                                "cache_rate": 0.23,
                                "cache_rate_denominator": 100,
                            },
                            "health": {"overall": "healthy", "cache_score": 0.9},
                        }
                    ]
                }
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        health = await fetch_group_health(
            Sub2APIInstanceConfig("test", "https://example.com", "secret-key"),
            client=client,
        )
    finally:
        await client.aclose()

    assert captured["key"] == "secret-key"
    assert "group_by=platform_group" in captured["url"]
    assert health[0].group_id == "1"
    assert health[0].cache_rate == 0.23
    assert health[0].cache_rate_denominator == 100
    assert health[0].overall == "healthy"


def test_build_report_returns_all_tied_minimum_groups_and_models():
    groups = [
        Sub2APIGroupRate("1", "标准组", "openai", "active", 1.0, ("gpt-4o",)),
        Sub2APIGroupRate("2", "低价组", "openai", "active", 0.5, ("gpt-4o-mini",)),
        Sub2APIGroupRate(
            "3", "另一个低价组", "anthropic", "active", 0.5, ("claude-3-5",)
        ),
    ]

    report = build_report(groups)
    assert report.minimum_multiplier == 0.5
    assert [group.name for group in report.minimum_groups] == ["低价组", "另一个低价组"]
    text = format_report("主站", report)
    assert "最低基础倍率：0.5x" in text
    assert "gpt-4o-mini" not in text
    assert "claude-3-5" not in text
    assert "最低倍率模型" not in text


def test_format_report_joins_health_by_group_id_and_hides_cache_when_sample_is_low():
    groups = [
        Sub2APIGroupRate("1", "健康组", "openai", "active", 1.0),
        Sub2APIGroupRate("2", "样本不足组", "openai", "active", 2.0),
    ]
    health = [
        Sub2APIGroupHealth("1", "健康组", "openai", "healthy", 0.23, 0.9, 100),
        Sub2APIGroupHealth(
            "2", "样本不足组", "openai", "warning", 0.9, 0.2, 4, minimum_sample=5
        ),
    ]

    text = format_report(
        "主站",
        build_report(groups),
        health=health,
        monitor_range="24h",
    )
    assert "健康组 | openai | 基础倍率：1x 渠道：健康 缓存率：23.0%" in text
    assert "样本不足组 | openai | 基础倍率：2x 渠道：警告 缓存率：无数据" in text
    assert "模型：" not in text


def test_format_report_keeps_multiplier_result_when_monitor_is_unavailable():
    group = Sub2APIGroupRate("1", "便宜组", "openai", "active", 0.025)
    text = format_report(
        "主站",
        build_report([group]),
        monitor_error="V2不可用",
    )
    assert "基础倍率：0.025x" in text
    assert "渠道状态：V2不可用" in text
    assert "https://" not in text
    assert "secret-key" not in text


def test_report_shows_peak_and_dynamic_multipliers_separately():
    group = Sub2APIGroupRate(
        "1",
        "高峰组",
        "openai",
        "active",
        1.0,
        peak_rate_enabled=True,
        peak_rate_multiplier=1.5,
        dynamic_rate_enabled=True,
        dynamic_rate_markup=1.2,
    )
    text = format_report("主站", build_report([group]))
    assert "高峰倍率：1.5x" in text
    assert "动态加成：1.2x" in text


def test_split_message_prefers_line_boundaries_and_enforces_limit():
    chunks = split_message("第一行\n第二行\n第三行", max_length=5)
    assert chunks == ["第一行", "第二行", "第三行"]
    assert all(len(chunk) <= 5 for chunk in chunks)

    long_chunks = split_message("abcdefghij", max_length=3)
    assert long_chunks == ["abc", "def", "ghi", "j"]


def test_build_auth_headers_prefers_the_requested_credential_then_falls_back():
    both = Sub2APIInstanceConfig("t", "https://example.com", "admin-key", "panel-jwt")
    assert (
        build_auth_headers(both, prefer_panel=True)["Authorization"]
        == "Bearer panel-jwt"
    )
    assert build_auth_headers(both, prefer_panel=False)["x-api-key"] == "admin-key"

    jwt_only = Sub2APIInstanceConfig("t", "https://example.com", "", "panel-jwt")
    assert build_auth_headers(jwt_only)["Authorization"] == "Bearer panel-jwt"
    assert "x-api-key" not in build_auth_headers(jwt_only)

    key_only = Sub2APIInstanceConfig("t", "https://example.com", "admin-key")
    assert build_auth_headers(key_only, prefer_panel=True)["x-api-key"] == "admin-key"
    assert "Authorization" not in build_auth_headers(key_only, prefer_panel=True)


def test_instance_has_credential_when_either_secret_is_present():
    assert Sub2APIInstanceConfig("t", "https://example.com", "k").has_credential
    assert Sub2APIInstanceConfig("t", "https://example.com", "", "j").has_credential
    assert not Sub2APIInstanceConfig("t", "https://example.com").has_credential


@pytest.mark.asyncio
async def test_fetch_groups_falls_back_to_bearer_when_no_admin_key_is_set():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"data": []}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await fetch_groups(
            Sub2APIInstanceConfig("test", "https://example.com", "", "panel-jwt"),
            client=client,
        )
    finally:
        await client.aclose()

    assert captured["auth"] == "Bearer panel-jwt"
    assert captured["key"] is None


@pytest.mark.asyncio
async def test_fetch_group_health_prefers_panel_jwt_over_admin_key():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"items": []}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await fetch_group_health(
            Sub2APIInstanceConfig(
                "test", "https://example.com", "admin-key", "panel-jwt"
            ),
            client=client,
        )
    finally:
        await client.aclose()

    assert captured["auth"] == "Bearer panel-jwt"
    assert captured["key"] is None


@pytest.mark.asyncio
async def test_fetch_group_health_parses_metrics_latency_and_buckets():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "platform": "openai",
                        "group_id": 1,
                        "group_name": "便宜组",
                        "metrics": {
                            "request_count": 1200,
                            "error_rate": 0.05,
                            "rpm": 3.5,
                            "cache_rate": 0.4,
                            "cache_rate_denominator": 800,
                            "ttft": {"avg_ms": 850.0, "p95_ms": 2100.0},
                        },
                        "health": {"overall": "healthy", "minimum_sample": 50},
                        "buckets": [
                            {
                                "bucket_start": "2026-09-23T10:00:00Z",
                                "metrics": {"request_count": 40, "error_rate": 0.0},
                                "health": {"overall": "healthy"},
                            },
                            {
                                "bucket_start": "2026-09-23T11:00:00Z",
                                "metrics": {"request_count": 0, "error_rate": 0.0},
                                "health": {"overall": "unknown"},
                            },
                        ],
                    }
                ]
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        health = await fetch_group_health(
            Sub2APIInstanceConfig("test", "https://example.com", "secret-key"),
            client=client,
        )
    finally:
        await client.aclose()

    item = health[0]
    assert item.request_count == 1200
    assert item.error_rate == pytest.approx(0.05)
    assert item.success_rate == pytest.approx(0.95)
    assert item.rpm == pytest.approx(3.5)
    assert item.ttft_avg_ms == pytest.approx(850.0)
    assert item.ttft_p95_ms == pytest.approx(2100.0)
    assert [bucket.overall for bucket in item.buckets] == ["healthy", "unknown"]
    assert item.buckets[0].is_empty is False
    assert item.buckets[1].is_empty is True


def test_success_rate_is_none_without_error_rate_and_clamps_out_of_range_values():
    assert (
        Sub2APIGroupHealth("1", "a", "openai", "healthy", None, None, 0).success_rate
        is None
    )
    assert (
        Sub2APIGroupHealth(
            "1", "a", "openai", "healthy", None, None, 0, error_rate=1.8
        ).success_rate
        == 0.0
    )
    assert (
        Sub2APIGroupHealth(
            "1", "a", "openai", "healthy", None, None, 0, error_rate=-0.5
        ).success_rate
        == 1.0
    )


def test_build_health_overview_counts_states_and_skips_untrustworthy_cache_rates():
    items = [
        Sub2APIGroupHealth(
            "1", "a", "openai", "healthy", 0.4, 0.9, 100, request_count=300
        ),
        Sub2APIGroupHealth(
            "2", "b", "openai", "warning", 0.6, 0.5, 100, request_count=200
        ),
        Sub2APIGroupHealth(
            "3", "c", "openai", "critical", 0.9, 0.1, 3, minimum_sample=50
        ),
        Sub2APIGroupHealth("4", "d", "openai", "unknown", None, None, 0),
    ]

    overview = build_health_overview(items)
    assert (overview.total, overview.healthy, overview.warning) == (4, 1, 1)
    assert (overview.critical, overview.unknown) == (1, 1)
    # 第三组的样板只有 3 次，低于 minimum_sample，不参与平均。
    assert overview.average_cache_rate == pytest.approx(0.5)
    assert overview.total_requests == 500


def test_build_health_overview_returns_none_average_when_no_group_qualifies():
    items = [
        Sub2APIGroupHealth(
            "1", "a", "openai", "unknown", 0.9, None, 2, minimum_sample=50
        )
    ]
    overview = build_health_overview(items)
    assert overview.average_cache_rate is None
    assert overview.total_requests == 0


def test_group_bucket_treats_zero_requests_as_empty():
    assert Sub2APIGroupBucket("t", 0, 0.0, "unknown").is_empty
    assert not Sub2APIGroupBucket("t", 1, 0.0, "healthy").is_empty


def _dashboard_module():
    return pytest.importorskip("dashboard")


def test_find_cjk_font_path_returns_none_when_no_candidate_matches(monkeypatch):
    dashboard = _dashboard_module()
    monkeypatch.setattr(dashboard, "FONT_CANDIDATES", ())
    monkeypatch.setattr(dashboard, "FONT_GLOBS", ())
    assert dashboard.find_cjk_font_path() is None


def test_render_dashboard_emits_png_bytes():
    dashboard = _dashboard_module()
    if not dashboard.PILLOW_AVAILABLE:
        pytest.skip("Pillow 未安装")
    if dashboard.find_cjk_font_path() is None:
        pytest.skip("系统里没有可用的中文字体")

    items = [
        Sub2APIGroupHealth(
            "1",
            "默认分组",
            "anthropic",
            "healthy",
            0.624,
            0.9,
            800,
            request_count=1200,
            error_rate=0.01,
            ttft_avg_ms=820.0,
            buckets=(
                Sub2APIGroupBucket("t1", 40, 0.0, "healthy"),
                Sub2APIGroupBucket("t2", 0, 0.0, "unknown"),
            ),
        ),
        Sub2APIGroupHealth("2", "试用分组", "openai", "critical", None, None, 0),
    ]

    png = dashboard.render_dashboard("主站", items, monitor_range="24h")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 1000


def test_render_dashboard_handles_an_empty_group_list():
    dashboard = _dashboard_module()
    if not dashboard.PILLOW_AVAILABLE:
        pytest.skip("Pillow 未安装")
    if dashboard.find_cjk_font_path() is None:
        pytest.skip("系统里没有可用的中文字体")

    png = dashboard.render_dashboard("主站", ())
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_dashboard_reports_missing_font(monkeypatch):
    dashboard = _dashboard_module()
    if not dashboard.PILLOW_AVAILABLE:
        pytest.skip("Pillow 未安装")

    monkeypatch.setattr(dashboard, "find_cjk_font_path", lambda: None)
    with pytest.raises(dashboard.DashboardUnavailable, match="中文字体"):
        dashboard.render_dashboard("主站", ())
