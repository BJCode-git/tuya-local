#!/usr/bin/env python3
# =============================================================================
# tuya_cloud_helper.py — client Tuya Cloud minimal & autonome pour robot LDS
# -----------------------------------------------------------------------------
# But : compléter tuya-local (100% local) par les 3 choses que le protocole
#       LOCAL Tuya n'expose pas proprement :
#         1. le binaire de la MAP (layout coloré + pièces)  → cloud
#         2. le nettoyage PAR PIÈCE (select_room + command_trans) → cloud
#         3. un fallback d'état si le local tombe             → cloud
#
# Ce module est ORIGINAL (signature HMAC-SHA256 = spec publique Tuya Open API).
# Il ne dépend que de `aiohttp`. Le DÉCODAGE de la map (bytes → image) est
# volontairement laissé à une lib dédiée (`tuya-vacuum`) ou à l'intégration
# Edsol — voir la note d'archi dans ma réponse : réinventer le décodeur est
# le maillon faible, autant réutiliser l'existant.
#
# Utilisation CLI (test rapide depuis un shell, hors HA) :
#   pip install aiohttp
#   export TUYA_ID=... TUYA_SECRET=... TUYA_DEVICE=... TUYA_REGION=eu
#   python3 tuya_cloud_helper.py status
#   python3 tuya_cloud_helper.py map    ./map.bin      # télécharge le binaire
#   python3 tuya_cloud_helper.py start
#   python3 tuya_cloud_helper.py dock
#   python3 tuya_cloud_helper.py rooms 0 2             # nettoie pièces 0 et 2
#
# Les identifiants (access_id / access_secret) sont ceux de TON projet Tuya
# IoT (Cloud > Authorization), le device_id celui du robot. Région = data
# center du compte de l'app (eu pour la France).
# =============================================================================

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

ENDPOINTS = {
    "eu": "https://openapi.tuyaeu.com",
    "us": "https://openapi.tuyaus.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}
_EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()
_TOKEN_SAFETY = 60  # refresh 60 s avant expiration


class TuyaCloud:
    """Client asynchrone minimal de l'API Tuya Cloud (robots LDS)."""

    def __init__(
        self,
        access_id: str,
        access_secret: str,
        device_id: str,
        region: str = "eu",
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._id = access_id
        self._secret = access_secret
        self._device = device_id
        self._base = ENDPOINTS.get(region, ENDPOINTS["eu"])
        self._session = session
        self._own_session = session is None
        self._token: Optional[str] = None
        self._token_exp: float = 0.0

    # ── cycle de vie ────────────────────────────────────────────────────────
    async def __aenter__(self) -> "TuyaCloud":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own_session and self._session:
            await self._session.close()
            self._session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    # ── signature ─────────────────────────────────────────────────────────────
    def _sign(self, method: str, path: str, token: str, body: str) -> dict[str, str]:
        """Construit les en-têtes signés HMAC-SHA256 (spec Tuya Open API v2)."""
        t = str(int(time.time() * 1000))
        body_hash = hashlib.sha256(body.encode()).hexdigest() if body else _EMPTY_BODY_SHA256
        # str_to_sign : client_id + [access_token] + t + method\n + body_sha256\n\n + path
        str_to_sign = (
            self._id
            + (token or "")
            + t
            + f"{method}\n{body_hash}\n\n{path}"
        )
        sign = (
            hmac.new(self._secret.encode(), str_to_sign.encode(), hashlib.sha256)
            .hexdigest()
            .upper()
        )
        headers = {
            "client_id": self._id,
            "sign": sign,
            "t": t,
            "sign_method": "HMAC-SHA256",
        }
        if token:
            headers["access_token"] = token
        return headers

    @staticmethod
    def _sorted_path(path: str) -> str:
        """Trie les paramètres de query par ordre alphabétique (exigence Tuya)."""
        if "?" not in path:
            return path
        base, query = path.split("?", 1)
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        return base + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    # ── token ─────────────────────────────────────────────────────────────────
    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - _TOKEN_SAFETY:
            return self._token
        path = "/v1.0/token?grant_type=1"
        headers = self._sign("GET", path, "", "")
        async with self._ensure_session().get(
            self._base + path, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json(content_type=None)
        if not data.get("success"):
            raise RuntimeError(f"Auth Tuya échouée : {data.get('msg') or data}")
        res = data["result"]
        self._token = res["access_token"]
        self._token_exp = time.time() + res.get("expire_time", 7200)
        return self._token

    # ── requêtes signées ──────────────────────────────────────────────────────
    async def _get(self, path: str) -> dict:
        token = await self._get_token()
        fp = self._sorted_path(path)
        headers = self._sign("GET", fp, token, "")
        async with self._ensure_session().get(
            self._base + fp, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return await resp.json(content_type=None)

    async def _post(self, path: str, body: dict) -> dict:
        token = await self._get_token()
        body_str = json.dumps(body, separators=(",", ":"))
        headers = self._sign("POST", path, token, body_str)
        headers["Content-Type"] = "application/json"
        async with self._ensure_session().post(
            self._base + path, headers=headers, data=body_str,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return await resp.json(content_type=None)

    async def _post_query(self, path: str) -> dict:
        """POST dont les paramètres sont dans la QUERY (pas de body).

        C'est le cas de `file-name` (renommage de map) : la doc Tuya met
        id/fileName en query et le body est vide.
        """
        token = await self._get_token()
        fp = self._sorted_path(path)
        headers = self._sign("POST", fp, token, "")
        headers["Content-Type"] = "application/json"
        async with self._ensure_session().post(
            self._base + fp, headers=headers, data="",
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return await resp.json(content_type=None)

    async def _delete(self, path: str) -> dict:
        token = await self._get_token()
        fp = self._sorted_path(path)
        headers = self._sign("DELETE", fp, token, "")
        async with self._ensure_session().delete(
            self._base + fp, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return await resp.json(content_type=None)

    # ── API device ─────────────────────────────────────────────────────────────
    async def get_status(self) -> list[dict]:
        """Liste [{code, value}] de tous les DP via cloud (fallback si local KO)."""
        res = await self._get(f"/v1.0/devices/{self._device}/status")
        return res.get("result", []) if res.get("success") else []

    async def send(self, commands: list[dict]) -> bool:
        """Envoie des commandes DP : [{'code': 'switch_go', 'value': True}]."""
        res = await self._post(
            f"/v1.0/devices/{self._device}/commands", {"commands": commands}
        )
        if not res.get("success"):
            _LOGGER.error("send() échec : %s / %s", res.get("code"), res.get("msg"))
        return bool(res.get("success"))

    # raccourcis de contrôle (utiles pour des automatisations cloud-side)
    async def start(self) -> bool:  return await self.send([{"code": "switch_go", "value": True}])
    async def pause(self) -> bool:  return await self.send([{"code": "pause", "value": True}])
    async def dock(self) -> bool:   return await self.send([{"code": "switch_charge", "value": True}])
    async def locate(self) -> bool: return await self.send([{"code": "seek", "value": True}])

    async def set_suction(self, level: str) -> bool:
        """level ∈ closed|gentle|normal|strong|max"""
        return await self.send([{"code": "suction", "value": level}])

    # ── MAP ─────────────────────────────────────────────────────────────────────
    async def get_realtime_map_urls(self) -> dict[int, str]:
        """URLs de map temps réel (dispo surtout pendant le nettoyage).
        map_type : 0=layout(pièces) 1=path 2=incrémental 3=planning."""
        res = await self._get(f"/v1.0/users/sweepers/file/{self._device}/realtime-map")
        if not res.get("success") or not res.get("result"):
            return {}
        return {i["map_type"]: i["map_url"] for i in res["result"]}

    async def list_maps(
        self,
        file_type: str = "pic",
        page_no: int = 1,
        page_size: int = 20,
    ) -> list[dict]:
        """Liste les fichiers de map (Sweeping Robot Open Service).

        file_type :
          - "pic"            -> sessions de map simples
          - "collect_recode" -> MULTI-MAP (les cartes d'étages de l'app)

        Renvoie [{id, time, extend, file_name}].

        [!] La doc Tuya documente les paramètres en snake_case (file_type,
        page_no, page_size) MAIS son exemple de requête les envoie en
        camelCase (fileType, pageNo, pageSize). On envoie LES DEUX : les
        paramètres inconnus sont ignorés côté Tuya, et ça rend l'appel
        robuste quelle que soit la convention réellement appliquée.
        """
        path = (
            f"/v1.0/users/sweepers/file/{self._device}/list"
            f"?device_id={self._device}"
            f"&file_type={file_type}&fileType={file_type}"
            f"&page_no={page_no}&pageNo={page_no}"
            f"&page_size={page_size}&pageSize={page_size}"
        )
        res = await self._get(path)
        if not res.get("success"):
            _LOGGER.debug("list_maps(%s) échec : %s", file_type, res.get("msg"))
            return []
        return res.get("result", {}).get("datas", [])

    async def list_multi_maps(self, page_size: int = 20) -> list[dict]:
        """Les cartes multi-étages telles que gérées par l'app."""
        return await self.list_maps(file_type="collect_recode", page_size=page_size)

    async def rename_map(self, file_id: int, name: str) -> bool:
        """Renomme une map — POST .../file-name?id=<id>&fileName=<nom>.

        Même remarque de nommage que list_maps : on envoie file_name ET
        fileName. Le nom est urlencodé (accents/espaces).
        """
        from urllib.parse import quote
        enc = quote(str(name), safe="")
        path = (
            f"/v1.0/users/sweepers/file/{self._device}/file-name"
            f"?id={int(file_id)}&file_name={enc}&fileName={enc}"
        )
        res = await self._post_query(path)
        if not res.get("success"):
            _LOGGER.error("rename_map échec : %s / %s", res.get("code"), res.get("msg"))
        return bool(res.get("success"))

    async def delete_maps(self, file_ids: list[int]) -> bool:
        """Supprime des maps — DELETE .../clean-history?fileIds=1,2,3."""
        ids = ",".join(str(int(i)) for i in file_ids)
        path = (
            f"/v1.0/users/sweepers/file/{self._device}/clean-history"
            f"?file_ids={ids}&fileIds={ids}"
        )
        res = await self._delete(path)
        if not res.get("success"):
            _LOGGER.error("delete_maps échec : %s / %s", res.get("code"), res.get("msg"))
        return bool(res.get("success"))

    async def _list_saved_maps(self, file_type: str = "pic", page_size: int = 1) -> list[dict]:
        """Compat interne (utilisé par get_map_bytes)."""
        return await self.list_maps(file_type=file_type, page_size=page_size)

    async def _saved_map_url(self, map_id: int) -> Optional[str]:
        res = await self._get(f"/v1.0/users/sweepers/file/{self._device}/download?id={map_id}")
        return res.get("result", {}).get("app_map") if res.get("success") else None

    async def _download(self, url: str) -> Optional[bytes]:
        async with self._ensure_session().get(
            url, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning("Téléchargement map HTTP %s", resp.status)
                return None
            return await resp.read()

    async def get_map_bytes(
        self, map_type: int = 0, file_id: int | None = None
    ) -> Optional[bytes]:
        """Binaire de la map pour un map_type donné.

        map_type : 0 = layout (pièces), 1 = path (trajectoire).
        file_id  : si fourni, télécharge CETTE carte enregistrée au lieu de
                   la carte courante. Sert à afficher une autre carte d'étage.

        Sans file_id : realtime-map d'abord (pendant le nettoyage), sinon
        repli sur le dernier fichier sauvegardé (robot au dock).
        None si aucune carte n'existe encore.
        """
        # Carte explicitement demandée -> pas de realtime, on va la chercher
        if file_id is not None:
            url = await self._saved_map_url(file_id)
            if not url:
                _LOGGER.warning("Pas d'URL de téléchargement pour la carte %s", file_id)
                return None
            return await self._download(url)

        rt = await self.get_realtime_map_urls()
        if map_type in rt:
            data = await self._download(rt[map_type])
            if data:
                return data
        if map_type != 0:
            return None  # le path n'a pas de fallback fichier sauvegardé
        saved = await self._list_saved_maps(page_size=1)
        if not saved:
            _LOGGER.debug("Aucune map disponible (device sans carto ou jamais nettoyé)")
            return None
        url = await self._saved_map_url(saved[0]["id"])
        return await self._download(url) if url else None

    async def send_command_trans(self, raw: bytes) -> bool:
        """Envoie une trame brute sur command_trans (DP15) — no-go zones, murs.
        La valeur d'un DP 'raw' Tuya se transmet en base64."""
        import base64
        b64 = base64.b64encode(raw).decode()
        return await self.send([{"code": "command_trans", "value": b64}])

    # ── nettoyage par pièce ────────────────────────────────────────────────────
    @staticmethod
    def _room_payload(room_id: int, passes: int = 1) -> str:
        """Payload command_trans (base64) pour sélectionner une pièce.
        Format 0xAA .. opcode 0x53 (confirmé sur cette famille LDS/乐动).
        room_id = index 0-based de la pièce (0 = Pièce 1)."""
        body = bytes([room_id, 0x01, 0x00, 0x01, 0x00, 0x00, 0x02,
                      0x00, 0x00, 0x00, 0xFF, passes & 0xFF])
        frame = bytes([0xAA, 0x00, len(body), 0x53, 0x01, 0x01]) + body
        checksum = 0
        for b in frame:
            checksum ^= b
        return base64.b64encode(frame + bytes([checksum])).decode()

    async def clean_rooms(self, room_ids: list[int], passes: int = 1) -> bool:
        """Nettoie des pièces : mode=select_room → command_trans(pièce) → switch_go.
        NB : le format binaire envoie une pièce à la fois ; le comportement
        multi-pièces dépend du firmware (à tester sur ton D10)."""
        if not room_ids:
            return False
        if not await self.send([{"code": "mode", "value": "select_room"}]):
            return False
        await asyncio.sleep(0.5)
        for rid in room_ids:
            await self.send([{"code": "command_trans", "value": self._room_payload(rid, passes)}])
            await asyncio.sleep(0.3)
        return await self.send([{"code": "switch_go", "value": True}])


# ── CLI de test ────────────────────────────────────────────────────────────────
async def _cli() -> None:
    import os
    import sys

    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cfg = dict(
        access_id=os.environ.get("TUYA_ID", ""),
        access_secret=os.environ.get("TUYA_SECRET", ""),
        device_id=os.environ.get("TUYA_DEVICE", ""),
        region=os.environ.get("TUYA_REGION", "eu"),
    )
    if not all([cfg["access_id"], cfg["access_secret"], cfg["device_id"]]):
        print("⚠ Renseigne TUYA_ID, TUYA_SECRET, TUYA_DEVICE (et TUYA_REGION).")
        return

    cmd, *rest = args
    async with TuyaCloud(**cfg) as tc:
        if cmd == "status":
            for dp in await tc.get_status():
                print(f"  {dp.get('code'):24} = {dp.get('value')}")
        elif cmd == "map":
            out = rest[0] if rest else "map.bin"
            data = await tc.get_map_bytes()
            if not data:
                print("Aucune map disponible.")
            else:
                with open(out, "wb") as f:
                    f.write(data)
                print(f"Map ({len(data)} octets) → {out}  "
                      f"(décode-la avec la lib tuya-vacuum ou l'intégration Edsol)")
        elif cmd == "start":  print("OK" if await tc.start() else "échec")
        elif cmd == "pause":  print("OK" if await tc.pause() else "échec")
        elif cmd == "dock":   print("OK" if await tc.dock() else "échec")
        elif cmd == "locate": print("OK" if await tc.locate() else "échec")
        elif cmd == "rooms":
            ids = [int(x) for x in rest]
            print("OK" if await tc.clean_rooms(ids) else "échec")
        else:
            print(f"Commande inconnue : {cmd}")
            print(__doc__)


if __name__ == "__main__":
    asyncio.run(_cli())
