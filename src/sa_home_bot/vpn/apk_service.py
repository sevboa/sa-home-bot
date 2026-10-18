from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ProtoError,
)
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.vpn import apk as apk_client
from sa_home_bot.vpn.service_common import (
    APK_API_TIMEOUT_S,
    APK_CHUNK_BYTES,
    APK_DOWNLOAD_TIMEOUT_S,
    APK_INFO_MEMO_S,
)


class ApkMixin:
    async def _apk_row(self) -> dict[str, Any] | None:
        cur = await self._db.conn.execute(
            "SELECT version, file_name, size, sha256, path, telegram_file_id, checked_at "
            "FROM vpn_apk WHERE id = 1"
        )
        row = await cur.fetchone()
        if row is None or row["path"] is None:
            return None
        return {
            "version": row["version"],
            "file_name": row["file_name"],
            "size": row["size"],
            "sha256": row["sha256"],
            "telegram_file_id": row["telegram_file_id"],
            "checked_at": row["checked_at"],
        }

    async def _download_apk(self, tag: str, asset: dict[str, Any]) -> None:
        url = str(asset.get("browser_download_url") or "")
        if not url:
            raise ProtoError(ERR_INTERNAL, "у релиза нет ссылки на APK")
        name = str(asset.get("name") or "amneziawg.apk")
        expected_size = int(asset.get("size") or 0)
        expected_sha = apk_client.asset_sha256(asset)

        cache_dir = self._cfg.apk_cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        dest = cache_dir / name
        tmp = cache_dir / f".{name}.part"
        await asyncio.to_thread(apk_client.download_sync, url, tmp, APK_DOWNLOAD_TIMEOUT_S)
        actual_size = tmp.stat().st_size
        if expected_size and actual_size != expected_size:
            tmp.unlink(missing_ok=True)
            raise ProtoError(
                ERR_INTERNAL,
                f"скачанный APK не совпал по размеру ({actual_size} != {expected_size})",
            )
        actual_sha = await asyncio.to_thread(apk_client.sha256_file, tmp)
        if expected_sha and expected_sha != actual_sha:
            tmp.unlink(missing_ok=True)
            raise ProtoError(ERR_INTERNAL, "скачанный APK не совпал по sha256")
        tmp.replace(dest)
        now = utc_now().isoformat()
        await self._db.conn.execute(
            "INSERT INTO vpn_apk (id, version, file_name, size, sha256, path, "
            "telegram_file_id, checked_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, NULL, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
            "file_name = excluded.file_name, size = excluded.size, sha256 = excluded.sha256, "
            "path = excluded.path, telegram_file_id = NULL, checked_at = excluded.checked_at, "
            "updated_at = excluded.updated_at",
            (tag, name, actual_size, actual_sha, str(dest), now, now),
        )
        await self._db.conn.commit()

    async def _apk_info(self, _args: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        if (
            self._apk_checked_at is not None
            and (now - self._apk_checked_at).total_seconds() < APK_INFO_MEMO_S
        ):
            row = await self._apk_row()
            if row is not None:
                return {**row, "stale": False}
        try:
            release = await asyncio.to_thread(
                apk_client.latest_release_sync, self._cfg.apk_repo, APK_API_TIMEOUT_S
            )
        except apk_client.ApkFetchError:
            row = await self._apk_row()
            if row is None:
                raise ProtoError(
                    ERR_INTERNAL, "APK ещё ни разу не скачан, а GitHub сейчас недоступен"
                ) from None
            return {**row, "stale": True}
        self._apk_checked_at = now
        tag = str(release.get("tag_name") or "")
        asset = apk_client.pick_apk_asset(release.get("assets") or [])
        if asset is None:
            row = await self._apk_row()
            if row is None:
                raise ProtoError(ERR_INTERNAL, "в последнем релизе не нашлось APK")
            return {**row, "stale": True}
        row = await self._apk_row()
        if row is not None and row.get("version") == tag and row.get("size") == asset.get("size"):
            return {**row, "stale": False}
        await self._download_apk(tag, asset)
        row = await self._apk_row()
        assert row is not None
        return {**row, "stale": False}

    async def _apk_chunk(self, args: dict[str, Any]) -> dict[str, Any]:
        offset = int(args.get("offset") or 0)
        length = int(args.get("length") or APK_CHUNK_BYTES)
        cur = await self._db.conn.execute("SELECT path, sha256, size FROM vpn_apk WHERE id = 1")
        row = await cur.fetchone()
        if row is None or row["path"] is None:
            raise ProtoError(ERR_BAD_REQUEST, "APK ещё не скачан — сначала apk_info")
        data = await asyncio.to_thread(apk_client.read_chunk, Path(row["path"]), offset, length)
        eof = offset + len(data) >= int(row["size"] or 0)
        return {
            "data_b64": base64.b64encode(data).decode(),
            "offset": offset,
            "eof": eof,
            "sha256": row["sha256"],
        }

    async def _apk_set_file_id(self, args: dict[str, Any]) -> dict[str, Any]:
        file_id = str(args.get("telegram_file_id") or "").strip()
        if not file_id:
            raise ProtoError(ERR_BAD_REQUEST, "не передан telegram_file_id")
        await self._db.conn.execute(
            "UPDATE vpn_apk SET telegram_file_id = ? WHERE id = 1", (file_id,)
        )
        await self._db.conn.commit()
        return {"ok": True}
