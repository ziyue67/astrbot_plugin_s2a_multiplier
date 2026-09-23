"""AstrBot plugin for querying Sub2API group model multipliers."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from .dashboard import PILLOW_AVAILABLE, DashboardUnavailable, render_dashboard
    from .multiplier_core import (
        MultiplierReport,
        Sub2APIError,
        Sub2APIGroupHealth,
        Sub2APIGroupRate,
        Sub2APIInstanceConfig,
        build_report,
        fetch_group_health,
        fetch_groups,
        format_report,
        split_message,
    )
except ImportError:  # Support loaders that execute main.py as a top-level module.
    from dashboard import PILLOW_AVAILABLE, DashboardUnavailable, render_dashboard
    from multiplier_core import (
        MultiplierReport,
        Sub2APIError,
        Sub2APIGroupHealth,
        Sub2APIGroupRate,
        Sub2APIInstanceConfig,
        build_report,
        fetch_group_health,
        fetch_groups,
        format_report,
        split_message,
    )


MAX_IMAGES_PER_COMMAND = 5
IMAGE_DIR_NAME = "astrbot_s2a_multiplier"


@dataclass(frozen=True)
class _CacheEntry:
    """Cached per-instance data.

    `groups` and `health` are fetched by different commands, so each half
    carries a `*_loaded` flag. Without it an entry written by one command
    would look like a cache hit for the other and silently return nothing.
    """

    expires_at: float = 0.0
    groups: tuple[Sub2APIGroupRate, ...] = ()
    health: tuple[Sub2APIGroupHealth, ...] = ()
    monitor_error: str | None = None
    groups_loaded: bool = False
    health_loaded: bool = False


@dataclass(frozen=True)
class _InstanceResult:
    instance: Sub2APIInstanceConfig
    report: MultiplierReport | None = None
    health: tuple[Sub2APIGroupHealth, ...] = ()
    monitor_error: str | None = None
    error: str | None = None


class Sub2APIMultiplierPlugin(Star):
    """Query active Sub2API groups and report their configured multipliers."""

    def __init__(self, context: Context, config: Mapping[str, Any]):
        super().__init__(context)
        self.config = config
        self.instances = _parse_instances(config.get("instances", []))
        self.cache_ttl_seconds = max(
            0.0, _as_float(config.get("cache_ttl_minutes"), 5.0) * 60
        )
        self.timeout_seconds = max(1.0, _as_float(config.get("timeout_seconds"), 10.0))
        self.max_message_chars = max(
            200, _as_int(config.get("max_message_chars"), 3000)
        )
        self.include_inactive = _as_bool(config.get("include_inactive"), False)
        self.monitor_enabled = _as_bool(config.get("monitor_enabled"), True)
        self.monitor_range = _normalize_monitor_range(config.get("monitor_range"))
        self.image_enabled = _as_bool(config.get("image_enabled"), True)
        self._cache: dict[str, _CacheEntry] = {}
        self._client: httpx.AsyncClient | None = None

    @filter.command("倍率")
    async def rate(self, event: AstrMessageEvent):
        """查询 Sub2API 各实例的模型倍率和最低倍率。"""

        for chunk in await self._query_chunks():
            yield event.plain_result(chunk)

    @filter.command("multiplier")
    async def multiplier(self, event: AstrMessageEvent):
        """查询 Sub2API 各实例的模型倍率和最低倍率。"""

        for chunk in await self._query_chunks():
            yield event.plain_result(chunk)

    @filter.command("渠道图")
    async def channel_image(self, event: AstrMessageEvent):
        """生成 Sub2API 渠道状态图。"""

        async for result in self._render_images(event):
            yield result

    @filter.command("channelimage")
    async def channel_image_english(self, event: AstrMessageEvent):
        """生成 Sub2API 渠道状态图。"""

        async for result in self._render_images(event):
            yield result

    async def _query_chunks(self) -> list[str]:
        if not self.instances:
            return ["Sub2API 倍率查询未配置实例，请先在插件配置中添加实例。"]

        client = await self._get_client()
        results = await asyncio.gather(
            *(self._query_instance(instance, client) for instance in self.instances),
            return_exceptions=False,
        )

        sections: list[str] = ["Sub2API 模型倍率查询"]
        for result in results:
            if result.report is not None:
                monitor_error = result.monitor_error
                if not self.monitor_enabled:
                    monitor_error = "V2不可用"
                elif monitor_error:
                    monitor_error = f"V2不可用（{monitor_error}）"
                sections.append(
                    format_report(
                        result.instance.name,
                        result.report,
                        health=result.health,
                        monitor_range=self.monitor_range
                        if self.monitor_enabled
                        else None,
                        monitor_error=monitor_error,
                    )
                )
            elif result.error:
                sections.append(f"【{result.instance.name}】查询失败：{result.error}")

        return split_message("\n\n".join(sections), self.max_message_chars)

    async def _query_instance(
        self,
        instance: Sub2APIInstanceConfig,
        client: httpx.AsyncClient,
    ) -> _InstanceResult:
        try:
            now = time.monotonic()
            cached = self._cache.get(instance.cache_key)
            if cached and cached.groups_loaded and cached.expires_at > now:
                return _InstanceResult(
                    instance=instance,
                    report=build_report(cached.groups),
                    health=cached.health,
                    monitor_error=cached.monitor_error,
                )

            tasks = [
                fetch_groups(
                    instance,
                    include_inactive=self.include_inactive,
                    timeout_seconds=self.timeout_seconds,
                    client=client,
                )
            ]
            if self.monitor_enabled:
                tasks.append(
                    fetch_group_health(
                        instance,
                        monitor_range=self.monitor_range,
                        timeout_seconds=self.timeout_seconds,
                        client=client,
                    )
                )
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            groups_result = responses[0]
            if isinstance(groups_result, Exception):
                raise groups_result
            groups = tuple(groups_result)

            health: tuple[Sub2APIGroupHealth, ...] = ()
            monitor_error: str | None = None
            if self.monitor_enabled:
                health_result = responses[1]
                if isinstance(health_result, Exception):
                    monitor_error = str(health_result)
                    logger.warning(
                        "Sub2API 渠道状态 V2 查询失败: instance=%s reason=%s",
                        instance.name,
                        monitor_error,
                    )
                else:
                    health = tuple(health_result)
            else:
                monitor_error = "V2不可用"

            if self.cache_ttl_seconds > 0:
                self._cache[instance.cache_key] = _CacheEntry(
                    expires_at=time.monotonic() + self.cache_ttl_seconds,
                    groups=groups,
                    health=health,
                    monitor_error=monitor_error,
                    groups_loaded=True,
                    health_loaded=True,
                )
            return _InstanceResult(
                instance=instance,
                report=build_report(groups),
                health=health,
                monitor_error=monitor_error,
            )
        except Sub2APIError as exc:
            logger.warning(
                "Sub2API 倍率查询失败: instance=%s reason=%s", instance.name, str(exc)
            )
            return _InstanceResult(instance=instance, error=str(exc))
        except Exception:  # noqa: BLE001 - a bad instance must not break the reply.
            logger.exception(
                "Sub2API 倍率查询发生未预期错误: instance=%s", instance.name
            )
            return _InstanceResult(instance=instance, error="插件内部处理失败")

    async def _render_images(self, event: AstrMessageEvent):
        """Yield one channel-status dashboard image per configured instance."""

        if not self.instances:
            yield event.plain_result(
                "Sub2API 渠道状态图未配置实例，请先在插件配置中添加实例。"
            )
            return
        if not self.image_enabled:
            yield event.plain_result("渠道状态图已在插件配置中关闭（image_enabled）。")
            return
        if not PILLOW_AVAILABLE:
            yield event.plain_result(
                "生成渠道状态图需要 Pillow，请先执行：pip install pillow"
            )
            return

        client = await self._get_client()
        rendered = 0
        for instance in self.instances:
            if rendered >= MAX_IMAGES_PER_COMMAND:
                yield event.plain_result(
                    f"实例数量超过 {MAX_IMAGES_PER_COMMAND} 个，本次只生成前 {MAX_IMAGES_PER_COMMAND} 张渠道状态图。"
                )
                break

            health, error = await self._fetch_health(instance, client)
            if error:
                yield event.plain_result(
                    f"【{instance.name}】渠道状态图生成失败：{error}"
                )
                continue
            if not health:
                yield event.plain_result(
                    f"【{instance.name}】没有可用的渠道监控数据（站点可能未启用 V2 被动监控）。"
                )
                continue

            try:
                png = render_dashboard(
                    instance.name,
                    health,
                    monitor_range=self.monitor_range,
                )
            except DashboardUnavailable as exc:
                yield event.plain_result(
                    f"【{instance.name}】渠道状态图生成失败：{exc}"
                )
                continue

            rendered += 1
            yield event.image_result(_write_image(instance, png))

    async def _fetch_health(
        self,
        instance: Sub2APIInstanceConfig,
        client: httpx.AsyncClient,
    ) -> tuple[tuple[Sub2APIGroupHealth, ...], str | None]:
        """Return channel health for one instance, reusing the shared cache."""

        now = time.monotonic()
        cached = self._cache.get(instance.cache_key)
        if cached and cached.health_loaded and cached.expires_at > now:
            return cached.health, cached.monitor_error

        try:
            health = tuple(
                await fetch_group_health(
                    instance,
                    monitor_range=self.monitor_range,
                    timeout_seconds=self.timeout_seconds,
                    client=client,
                )
            )
        except Sub2APIError as exc:
            logger.warning(
                "Sub2API 渠道状态查询失败: instance=%s reason=%s",
                instance.name,
                str(exc),
            )
            return (), str(exc)
        except Exception:  # noqa: BLE001 - a bad instance must not break the reply.
            logger.exception(
                "Sub2API 渠道状态查询发生未预期错误: instance=%s", instance.name
            )
            return (), "插件内部处理失败"

        if self.cache_ttl_seconds > 0:
            base = cached if cached else _CacheEntry()
            self._cache[instance.cache_key] = _CacheEntry(
                expires_at=time.monotonic() + self.cache_ttl_seconds,
                groups=base.groups,
                health=health,
                monitor_error=None,
                groups_loaded=base.groups_loaded,
                health_loaded=True,
            )
        return health, None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.timeout_seconds,
            )
        return self._client

    async def terminate(self):
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        self._cache.clear()


def _parse_instances(raw_instances: Any) -> tuple[Sub2APIInstanceConfig, ...]:
    if not isinstance(raw_instances, list):
        return ()

    instances: list[Sub2APIInstanceConfig] = []
    for index, raw in enumerate(raw_instances, start=1):
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name") or f"Sub2API-{index}").strip()
        base_url = str(raw.get("base_url") or "").strip()
        api_key = str(raw.get("admin_api_key") or "").strip()
        panel_jwt = str(raw.get("panel_jwt") or "").strip()
        if not base_url or not (api_key or panel_jwt):
            logger.warning(
                "跳过未完整配置的 Sub2API 实例（需要 base_url 以及管理员 API Key 或面板 JWT）: instance=%s",
                name,
            )
            continue
        instances.append(
            Sub2APIInstanceConfig(
                name=name,
                base_url=base_url,
                admin_api_key=api_key,
                panel_jwt=panel_jwt,
            )
        )
    return tuple(instances)


def _write_image(instance: Sub2APIInstanceConfig, png: bytes) -> str:
    """Write a rendered dashboard to a stable temp path and return it.

    The message payload references the file by path, so the caller must not
    delete it before the platform finishes sending. Reusing one filename per
    instance keeps the directory from growing without bound.
    """

    directory = os.path.join(tempfile.gettempdir(), IMAGE_DIR_NAME)
    os.makedirs(directory, exist_ok=True)
    slug = re.sub(r"[^0-9A-Za-z_-]+", "_", instance.name).strip("_")[:24] or "instance"
    digest = hashlib.sha1(instance.cache_key.encode("utf-8")).hexdigest()[:8]
    path = os.path.join(directory, f"{slug}-{digest}.png")
    with open(path, "wb") as handle:
        handle.write(png)
    return path


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_monitor_range(value: Any) -> str:
    selected = str(value or "24h").strip().lower()
    return selected if selected in {"90m", "24h", "7d", "30d"} else "24h"
