"""
Services carte pour tuya-local (extension).

DEUX FAMILLES, STRICTEMENT SÉPARÉES :

  LOCAL (toujours disponible, AUCUN appel réseau sortant)
    tuya_local.set_no_go_zones     -> écrit une trame sur le dp 15 (command_trans)
    tuya_local.set_virtual_walls   -> idem
    tuya_local.clean_rooms         -> mode=select_room puis trame dp 15 puis dp 1
  Ces trois services n'utilisent QUE le protocole local tuya-local. C'est
  possible parce que `command_trans` est un dp local : le cloud n'est requis
  que pour LIRE la carte, jamais pour la piloter.

  CLOUD (enregistrés uniquement si l'utilisateur a activé le cloud)
    tuya_local.rename_map / delete_map / refresh_map

Si le cloud est refusé dans les options, les services cloud ne sont pas
enregistrés du tout — ils n'apparaissent même pas dans l'UI.
"""

from __future__ import annotations

import base64
import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from . import map_protocol as proto
from .const import CONF_CLOUD_MAP, DOMAIN

_LOGGER = logging.getLogger(__name__)

# dps utilisés côté local
DP_SWITCH_GO = "1"
DP_MODE = "4"
DP_COMMAND_TRANS = "15"

SERVICE_SET_NO_GO = "set_no_go_zones"
SERVICE_SET_WALLS = "set_virtual_walls"
SERVICE_CLEAN_ROOMS = "clean_rooms"
SERVICE_RENAME_MAP = "rename_map"
SERVICE_DELETE_MAP = "delete_map"
SERVICE_REFRESH_MAP = "refresh_map"
SERVICE_LIST_MAPS = "list_maps"
SERVICE_SELECT_MAP = "select_map"
SERVICE_ZONE_CLEAN = "clean_zones"
SERVICE_GO_TO = "go_to_point"
SERVICE_SAVE_MAP = "save_map"
SERVICE_CLEAR_AREAS = "clear_areas"
SERVICE_USE_MAP = "use_map"

LOCAL_SERVICES = (
    SERVICE_SET_NO_GO,
    SERVICE_SET_WALLS,
    SERVICE_CLEAN_ROOMS,
    SERVICE_ZONE_CLEAN,
    SERVICE_GO_TO,
    SERVICE_SAVE_MAP,
    SERVICE_CLEAR_AREAS,
)
CLOUD_SERVICES = (
    SERVICE_RENAME_MAP,
    SERVICE_DELETE_MAP,
    SERVICE_REFRESH_MAP,
    SERVICE_LIST_MAPS,
    SERVICE_SELECT_MAP,
    SERVICE_USE_MAP,
)

# L'origine et l'échelle viennent de la carte décodée si elles ne sont pas
# fournies : la card n'a donc rien à calculer.
_GEOM = {
    vol.Optional("origin_x"): vol.Coerce(float),
    vol.Optional("origin_y"): vol.Coerce(float),
    vol.Optional("map_scale", default=1): vol.Coerce(int),
}
_ZONES_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Required("zones"): list,
        vol.Optional("forbid_type"): vol.In(["all", "sweep", "mop"]),
        **_GEOM,
    }
)
_WALLS_SCHEMA = vol.Schema(
    {vol.Optional("device_id"): cv.string, vol.Required("walls"): list, **_GEOM}
)
_CLEAN_ZONES_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Required("zones"): list,
        vol.Optional("passes"): vol.Coerce(int),
        **_GEOM,
    }
)
_GOTO_SCHEMA = vol.Schema(
    {vol.Optional("device_id"): cv.string, vol.Required("point"): list, **_GEOM}
)
_ROOMS_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Required("room_ids"): vol.All(cv.ensure_list, [vol.Coerce(int)]),
        vol.Optional("passes", default=1): vol.Coerce(int),
    }
)
_SAVE_SCHEMA = vol.Schema({vol.Optional("device_id"): cv.string})
_CLEAR_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Optional("kind"): vol.In(["NO_GO_ZONE", "VIRTUAL_WALL", "ZONE_CLEAN"]),
        # index d'une zone precise dans son type ; absent = tout le type
        vol.Optional("index"): vol.Coerce(int),
        vol.Optional("source", default="stored"): vol.In(["stored", "map"]),
    }
)
_USE_MAP_SCHEMA = vol.Schema(
    {vol.Optional("device_id"): cv.string, vol.Required("map_id"): vol.Coerce(int)}
)
_RENAME_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Required("map_id"): vol.Coerce(int),
        vol.Required("name"): cv.string,
    }
)
_DELETE_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Required("map_ids"): vol.All(cv.ensure_list, [vol.Coerce(int)]),
    }
)
_REFRESH_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        # False = ne pas redemander au robot, se contenter de retélécharger
        vol.Optional("ask_device", default=True): bool,
    }
)
_LIST_SCHEMA = vol.Schema({vol.Optional("device_id"): cv.string})
_SELECT_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        # l'un OU l'autre ; aucun des deux = revenir à la carte courante
        vol.Optional("map_id"): vol.Coerce(int),
        vol.Optional("name"): cv.string,
    }
)


def _resolve_device_id(hass: HomeAssistant, data: dict) -> str:
    """device_id explicite, sinon l'unique robot configuré.

    Évite d'avoir à renseigner l'identifiant quand il n'y a qu'un aspirateur,
    ce qui est le cas courant.
    """
    explicit = data.get("device_id")
    if explicit:
        return explicit
    store = hass.data.get(DOMAIN) or {}
    candidates = [
        key
        for key, value in store.items()
        if isinstance(value, dict) and value.get("map_camera") is not None
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise HomeAssistantError(
            "Aucun robot avec une carte n'est configuré. Active les fonctions "
            "cloud dans les options de l'appareil."
        )
    raise HomeAssistantError(
        f"Plusieurs robots configurés ({len(candidates)}) : précise device_id."
    )


def _get_device(hass: HomeAssistant, device_id: str):
    """Récupère l'objet TuyaLocalDevice depuis le cache de l'intégration."""
    data = (hass.data.get(DOMAIN) or {}).get(device_id)
    if not data or not data.get("device"):
        raise HomeAssistantError(
            f"Appareil tuya_local inconnu : {device_id}. "
            "Utilise l'identifiant de l'appareil (device_id de la config)."
        )
    return data["device"]


def _camera_of(hass: HomeAssistant, device_id: str):
    """Récupère l'entité caméra carte, ou explique pourquoi elle n'existe pas."""
    data = (hass.data.get(DOMAIN) or {}).get(device_id) or {}
    cam = data.get("map_camera")
    if cam is None:
        raise HomeAssistantError(
            "Aucune entité carte pour cet appareil. Vérifie que les fonctions "
            "cloud sont activées dans les options de l'appareil et que "
            "Home Assistant a bien été redémarré."
        )
    return cam


def _cloud_of(hass: HomeAssistant, device_id: str):
    data = (hass.data.get(DOMAIN) or {}).get(device_id) or {}
    cloud = data.get("cloud_map")
    if cloud is None:
        raise HomeAssistantError(
            "Les fonctions cloud sont désactivées pour cet appareil. "
            "Active-les dans Paramètres > Appareils et services > "
            "tuya_local > [appareil] > Configurer."
        )
    return cloud


def _remember(hass: HomeAssistant, device_id: str, kind: str, points_list) -> None:
    """Mémorise une zone posée pour qu'elle reste visible sur la carte."""
    data = (hass.data.get(DOMAIN) or {}).get(device_id) or {}
    cam = data.get("map_camera")
    if cam is not None and hasattr(cam, "remember_area"):
        try:
            cam.remember_area(kind, points_list)
        except Exception as e:
            _LOGGER.debug("Zone non mémorisée : %s", e)


async def _write_frame(hass: HomeAssistant, device_id: str, frame: bytes) -> None:
    """Écrit une trame binaire sur le dp 15, en LOCAL."""
    device = _get_device(hass, device_id)
    payload = base64.b64encode(frame).decode()
    _LOGGER.debug("dp15 <- %s (%d octets)", frame.hex(), len(frame))
    await device.async_set_property(DP_COMMAND_TRANS, payload)


def _geometry(hass: HomeAssistant, data: dict) -> tuple[tuple[float, float], int]:
    """Origine et échelle : celles fournies, sinon celles de la carte décodée."""
    scale = int(data.get("map_scale", 1))
    if "origin_x" in data and "origin_y" in data:
        return (float(data["origin_x"]), float(data["origin_y"])), scale
    try:
        cam = _camera_of(hass, data["device_id"])
        layout = getattr(cam, "layout", None)
        if layout is not None:
            return (
                float(getattr(layout, "origin_x", 0.0)),
                float(getattr(layout, "origin_y", 0.0)),
            ), scale
    except HomeAssistantError:
        pass
    _LOGGER.debug("Origine inconnue, (0, 0) utilisée")
    return (0.0, 0.0), scale


async def async_setup_map_services(hass: HomeAssistant, config: dict) -> None:
    """Enregistre les services. Les services cloud sont conditionnels."""

    async def _set_no_go(call: ServiceCall) -> None:
        origin, scale = _geometry(hass, call.data)
        for zone in call.data["zones"]:
            if len(zone) != 4:
                raise HomeAssistantError("Une no-go zone = exactement 4 coins.")
        frame = proto.encode_no_go_zones(
            call.data["zones"],
            origin=origin,
            map_scale=scale,
            forbid_type=call.data.get("forbid_type"),
        )
        await _write_frame(hass, _resolve_device_id(hass, call.data), frame)
        _remember(hass, _resolve_device_id(hass, call.data), "NO_GO_ZONE", call.data["zones"])

    async def _set_walls(call: ServiceCall) -> None:
        origin, scale = _geometry(hass, call.data)
        for wall in call.data["walls"]:
            if len(wall) != 2:
                raise HomeAssistantError("Un mur virtuel = exactement 2 points.")
        frame = proto.encode_virtual_walls(
            call.data["walls"], origin=origin, map_scale=scale
        )
        await _write_frame(hass, _resolve_device_id(hass, call.data), frame)
        _remember(hass, _resolve_device_id(hass, call.data), "VIRTUAL_WALL", call.data["walls"])

    async def _clean_zones(call: ServiceCall) -> None:
        """Nettoyage d'une zone dessinée (划区清扫)."""
        origin, scale = _geometry(hass, call.data)
        device_id = _resolve_device_id(hass, call.data)
        device = _get_device(hass, device_id)
        await device.async_set_property(DP_MODE, "zone")
        frame = proto.encode_zone_clean(
            call.data["zones"],
            origin=origin,
            map_scale=scale,
            passes=call.data.get("passes"),
        )
        await _write_frame(hass, device_id, frame)
        await device.async_set_property(DP_SWITCH_GO, True)
        _remember(hass, device_id, "ZONE_CLEAN", call.data["zones"])

    async def _go_to_point(call: ServiceCall) -> None:
        """Envoie le robot à un point (指哪扫哪)."""
        origin, scale = _geometry(hass, call.data)
        device_id = _resolve_device_id(hass, call.data)
        device = _get_device(hass, device_id)
        point = call.data["point"]
        if len(point) != 2:
            raise HomeAssistantError("Un point = [x, y].")
        await device.async_set_property(DP_MODE, "pose")
        frame = proto.encode_go_to_point(
            (point[0], point[1]), origin=origin, map_scale=scale
        )
        await _write_frame(hass, device_id, frame)
        await device.async_set_property(DP_SWITCH_GO, True)

    async def _clean_rooms(call: ServiceCall) -> None:
        """Nettoyage par pièce, 100% local : mode -> trame 0x14 -> start."""
        device_id = _resolve_device_id(hass, call.data)
        device = _get_device(hass, device_id)
        await device.async_set_property(DP_MODE, "select_room")
        frame = proto.encode_room_clean(
            call.data["room_ids"], call.data.get("passes", 1)
        )
        await _write_frame(hass, device_id, frame)
        await device.async_set_property(DP_SWITCH_GO, True)

    async def _save_map(call: ServiceCall) -> None:
        """Enregistre la carte courante comme carte multi-étages."""
        await _write_frame(
            hass, _resolve_device_id(hass, call.data), proto.encode_save_map()
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SET_NO_GO, _set_no_go, schema=_ZONES_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_WALLS, _set_walls, schema=_WALLS_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAN_ROOMS, _clean_rooms, schema=_ROOMS_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ZONE_CLEAN, _clean_zones, schema=_CLEAN_ZONES_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_GO_TO, _go_to_point, schema=_GOTO_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SAVE_MAP, _save_map, schema=_SAVE_SCHEMA)

    async def _clear_areas(call: ServiceCall) -> None:
        cam = _camera_of(hass, _resolve_device_id(hass, call.data))
        try:
            cam.forget_areas(
                call.data.get("kind"),
                call.data.get("index"),
                call.data.get("source", "stored"),
            )
        except ValueError as e:
            raise HomeAssistantError(str(e)) from e

    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_AREAS, _clear_areas, schema=_CLEAR_SCHEMA
    )

    # ── services cloud : uniquement si l'utilisateur les a acceptés ───────────
    if not config.get(CONF_CLOUD_MAP):
        _LOGGER.debug("Cloud désactivé : services de gestion de carte non exposés")
        return

    async def _rename_map(call: ServiceCall) -> None:
        cloud = _cloud_of(hass, _resolve_device_id(hass, call.data))
        async with cloud:
            ok = await cloud.rename_map(call.data["map_id"], call.data["name"])
        if not ok:
            raise HomeAssistantError(
                "Renommage refusé. Vérifie que 'Sweeping Robot Open Service' "
                "est autorisé sur ton projet Tuya IoT."
            )

    async def _delete_map(call: ServiceCall) -> None:
        cloud = _cloud_of(hass, _resolve_device_id(hass, call.data))
        async with cloud:
            ok = await cloud.delete_maps(call.data["map_ids"])
        if not ok:
            raise HomeAssistantError("Suppression refusée par le cloud Tuya.")

    async def _refresh_map(call: ServiceCall) -> None:
        """Actualise la carte SANS l'effacer.

        Demande au robot de re-publier sa carte (dp 16 = get_both, écriture
        locale), puis retélécharge et redécode. Ce n'est PAS une remise à zéro :
        l'effacement, c'est dp 13 (map_reset), jamais utilisé ici.
        """
        cam = _camera_of(hass, _resolve_device_id(hass, call.data))
        await cam.async_refresh_map(ask_device=call.data.get("ask_device", True))

    async def _list_maps(call: ServiceCall) -> dict:
        """Liste les cartes enregistrées et renvoie le résultat au frontend."""
        cam = _camera_of(hass, _resolve_device_id(hass, call.data))
        maps = await cam.async_reload_map_list()
        return {"maps": maps, "count": len(maps)}

    async def _select_map(call: ServiceCall) -> dict:
        """Choisit la carte à afficher (par id ou par nom).

        Sans map_id ni name : retour à la carte courante (temps réel).
        """
        cam = _camera_of(hass, _resolve_device_id(hass, call.data))
        map_id = call.data.get("map_id")
        name = call.data.get("name")

        if map_id is None and name:
            if not cam.maps:
                await cam.async_reload_map_list()
            wanted = name.strip().casefold()
            match = [m for m in cam.maps if m["name"].strip().casefold() == wanted]
            if not match:
                raise HomeAssistantError(
                    f"Carte '{name}' introuvable. Disponibles : "
                    f"{[m['name'] for m in cam.maps]}"
                )
            map_id = match[0]["id"]

        try:
            await cam.async_select_map(map_id)
        except ValueError as e:
            raise HomeAssistantError(str(e)) from e
        return {"selected_map_id": map_id}

    hass.services.async_register(
        DOMAIN, SERVICE_RENAME_MAP, _rename_map, schema=_RENAME_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_MAP, _delete_map, schema=_DELETE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_MAP, _refresh_map, schema=_REFRESH_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_MAPS,
        _list_maps,
        schema=_LIST_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    async def _use_map(call: ServiceCall) -> None:
        """Bascule le ROBOT sur une carte enregistrée (multi-étages).

        À distinguer de select_map, qui ne change que la carte AFFICHÉE.
        Ici on récupère l'URL du fichier via le cloud, puis on l'envoie au
        robot avec la trame 0x2e : c'est lui qui la télécharge.
        """
        device_id = _resolve_device_id(hass, call.data)
        map_id = call.data["map_id"]
        cloud = _cloud_of(hass, device_id)
        async with cloud:
            url = await cloud._saved_map_url(map_id)
        if not url:
            raise HomeAssistantError(
                f"Pas d'URL de téléchargement pour la carte {map_id}."
            )
        await _write_frame(hass, device_id, proto.encode_use_map(map_id, url))

    hass.services.async_register(
        DOMAIN, SERVICE_USE_MAP, _use_map, schema=_USE_MAP_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SELECT_MAP,
        _select_map,
        schema=_SELECT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
