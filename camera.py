"""
Platform for Tuya Cameras
"""

import logging

from homeassistant.components.camera import Camera as CameraEntity
from homeassistant.components.camera import CameraEntityFeature

from .const import CONF_CLOUD_MAP, CONF_TYPE, DOMAIN
from .device import TuyaLocalDevice
from .entity import TuyaLocalEntity
from .helpers.config import get_device_id
from .helpers.device_config import TuyaEntityConfig, get_config

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up camera entities.

    Two kinds of camera entity are handled here:

      * standard Tuya cameras -> TuyaLocalCamera (unchanged upstream behaviour)
      * robot vacuum map      -> TuyaLocalMapCamera, recognised by a dp named
                                 "map". This one needs the Tuya cloud, so it is
                                 only created when the user has explicitly
                                 enabled cloud features for the device.

    A per-entity loop is used instead of async_tuya_setup_platform because the
    class to instantiate depends on the entity, and because the map camera has
    to be skippable without failing the whole platform.
    """
    config = {**config_entry.data, **config_entry.options}
    cloud_enabled = bool(config.get(CONF_CLOUD_MAP))

    cfg = await hass.async_add_executor_job(get_config, config[CONF_TYPE])
    if cfg is None:
        raise ValueError(f"No device config found for {config}")

    data = hass.data[DOMAIN][get_device_id(config)]
    device = data["device"]

    entities = []
    for ecfg in cfg.all_entities():
        if ecfg.entity != "camera":
            continue
        try:
            if any(d.name == "map" for d in ecfg.dps()):
                if not cloud_enabled:
                    _LOGGER.debug(
                        "Cloud features disabled: skipping map camera %s",
                        ecfg.config_id,
                    )
                    continue
                entity = TuyaLocalMapCamera(device, ecfg)
            else:
                entity = TuyaLocalCamera(device, ecfg)
        except Exception as e:
            _LOGGER.error("Error adding %s: %s", ecfg.config_id, e)
            # La plateforme a été forwardée : on doit laisser une trace pour
            # qu'elle soit déchargée, sous peine de casser le rechargement.
            data[ecfg.config_id] = None
            continue
        data[ecfg.config_id] = entity
        entities.append(entity)

    if entities:
        async_add_entities(entities)


class TuyaLocalCamera(TuyaLocalEntity, CameraEntity):
    """Representation of a Tuya Camera"""

    def __init__(self, device: TuyaLocalDevice, config: TuyaEntityConfig):
        """
        Initialise the camera.
        Args:
            device (TuyaLocalDevice): the device API instance
            config (TuyaEntityConfig): the configuration for this entity
        """
        super().__init__()
        dps_map = self._init_begin(device, config)
        self._switch_dp = dps_map.pop("switch", None)
        self._snapshot_dp = dps_map.pop("snapshot", None)
        self._record_dp = dps_map.pop("record", None)
        self._motion_enable_dp = dps_map.pop("motion_enable", None)

        self._init_end(dps_map)
        if self._switch_dp:
            self._attr_supported_features |= CameraEntityFeature.ON_OFF

    @property
    def is_recording(self):
        """Return whether the camera is recording, if we know that."""
        if self._record_dp:
            return self._record_dp.get_value(self._device)

    @property
    def motion_detection_enabled(self):
        """Return whether motion detection is enabled if supported."""
        if self._motion_enable_dp:
            return self._motion_enable_dp.get_value(self._device)

    async def async_camera_image(self, width=None, height=None):
        if self._snapshot_dp:
            _LOGGER.info("%s fetching snapshot", self._config.config_id)
            return self._snapshot_dp.decoded_value(self._device)

    @property
    def is_on(self):
        """Return the power state of the camera"""
        if self._switch_dp:
            return self._switch_dp.get_value(self._device)

    async def async_turn_off(self):
        """Turn off the camera"""
        if not self._switch_dp:
            raise NotImplementedError()
        _LOGGER.info("%s turning off camera", self._config.config_id)
        await self._switch_dp.async_set_value(self._device, False)

    async def async_turn_on(self):
        """Turn on the camera"""
        if not self._switch_dp:
            raise NotImplementedError()
        _LOGGER.info("%s turning on camera", self._config.config_id)
        await self._switch_dp.async_set_value(self._device, True)

    async def async_enable_motion_detection(self):
        """Enable motion detection on the camera"""
        if not self._motion_enable_dp:
            raise NotImplementedError()
        _LOGGER.info("%s enabling motion detection", self._config.config_id)
        await self._motion_enable_dp.async_set_value(self._device, True)

    async def async_disable_motion_detection(self):
        """Disable motion detection on the camera"""
        if not self._motion_enable_dp:
            raise NotImplementedError()
        _LOGGER.info("%s disabling motion detection", self._config.config_id)
        await self._motion_enable_dp.async_set_value(self._device, False)


# =============================================================================
#  Robot vacuum map camera (cloud)
# -----------------------------------------------------------------------------
#  This is the ONLY part of the integration that talks to the Tuya cloud, and
#  it is opt-in: without CONF_CLOUD_MAP the entity above is never created and
#  nothing here runs.
#
#  Why the cloud is unavoidable here: the laser vacuum firmware uploads its
#  map (lay.bin / rou.bin) to Tuya object storage and only publishes
#  incremental frames over the local protocol. Reading the map therefore needs
#  the cloud. Controlling it does not - no-go zones, virtual walls and room
#  cleaning are written to dp 15 locally, see map_services.py.
# =============================================================================


class TuyaLocalMapCamera(TuyaLocalEntity, CameraEntity):
    """Robot vacuum map, rendered as a PNG from Tuya cloud data."""

    # Minimum delay between two map downloads, to protect low-power hosts
    # and the cloud API quota.
    MIN_REFRESH_SECONDS = 60

    # The official panel derives the robot position from the last point of the
    # path data (homeMapView.tsx: curPos = pathData[pathData.length - 1]);
    # there is no dedicated position dp. The robot only publishes path frames
    # when asked, so while it is active we ask it periodically. This is a tiny
    # LOCAL write, no cloud involved.
    POSITION_POLL_SECONDS = 10
    ACTIVE_STATES = (
        "smart",
        "zone_clean",
        "part_clean",
        "cleaning",
        "select_room",
        "goto_pos",
        "goto_charge",
        "spot_clean",
        "matrix_clean",
    )

    def __init__(self, device: TuyaLocalDevice, config: TuyaEntityConfig):
        """Initialise the map camera."""
        super().__init__()
        dps_map = self._init_begin(device, config)
        self._map_dp = dps_map.pop("map", None)
        self._init_end(dps_map)

        self._png = None
        self._layout = None
        self._last_fetch = 0.0
        self._cloud = None
        self._cloud_path = {}      # trajectoire du dernier telechargement cloud
        self._map_areas = []       # zones lues dans area_info.json de l'archive
        self._hidden_areas = set() # zones cloud masquées après suppression
        self._archive_probe = {}   # diagnostic : tete de chaque membre d'archive
        self._zones = {}           # zones posees, memorisees pour l'affichage
        self._maps = []            # cartes enregistrees (multi-etages)
        self._selected_map_id = None   # None = carte courante (realtime/derniere)
        self._last_error = None
        self._poll_unsub = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        store = self._device._hass.data.get(DOMAIN, {})
        for entry in store.values():
            if isinstance(entry, dict) and entry.get("device") is self._device:
                self._cloud = entry.get("cloud_map")
                entry["map_camera"] = self
                break
        try:
            await self._restore_zones()
            self._start_position_polling()
        except Exception as e:  # noqa: BLE001
            # Un incident sur ces extras ne doit pas empêcher l'entité
            # d'exister : sinon la plateforme reste chargée sans entité et le
            # rechargement suivant échoue.
            _LOGGER.warning("Initialisation partielle de la carte : %s", e)
        if self._cloud is None:
            _LOGGER.warning(
                "Map camera enabled but no cloud client configured - check the "
                "Tuya IoT credentials in the device options"
            )

    async def async_will_remove_from_hass(self):
        if self._poll_unsub:
            self._poll_unsub()
            self._poll_unsub = None
        await super().async_will_remove_from_hass()

    def _start_position_polling(self):
        """Demande la trajectoire à intervalle régulier quand le robot bouge."""
        from datetime import timedelta

        from homeassistant.helpers.event import async_track_time_interval

        if self._poll_unsub or self._map_dp is None:
            return

        async def _tick(_now):
            try:
                status = self._device.get_property("5")
            except Exception:
                return
            if status not in self.ACTIVE_STATES:
                return
            try:
                # get_path suffit : on ne veut que la trajectoire, pas la carte
                await self._map_dp.async_set_value(self._device, "get_path")
            except Exception as e:
                _LOGGER.debug("Demande de trajectoire impossible : %s", e)

        self._poll_unsub = async_track_time_interval(
            self._device._hass, _tick, timedelta(seconds=self.POSITION_POLL_SECONDS)
        )
        _LOGGER.debug(
            "Suivi de position actif : trajectoire demandée toutes les %s s "
            "pendant le nettoyage",
            self.POSITION_POLL_SECONDS,
        )

    async def async_camera_image(self, width=None, height=None):
        await self._maybe_refresh()
        return self._png

    # ------------------------------------------------------------------
    #  Map management
    # ------------------------------------------------------------------
    @property
    def layout(self):
        """Dernier layout décodé (origine, échelle, pièces)."""
        return self._layout

    @property
    def maps(self):
        """Stored maps, as [{id, name, time}]."""
        return self._maps

    @property
    def selected_map_id(self):
        """Currently displayed stored map, or None for the live map."""
        return self._selected_map_id

    async def async_refresh_map(self, ask_device: bool = True):
        """Refresh the map without erasing anything.

        Two distinct things happen here:

          1. optionally ask the robot to re-publish its current map, by
             writing "get_both" to the request dp (dp 16). This is a LOCAL
             write and is purely a request to upload - it does NOT reset the
             map. Erasing is dp 13 (map_reset) and is never used here.
          2. re-download and decode from the cloud.
        """
        if ask_device and self._map_dp is not None:
            try:
                await self._map_dp.async_set_value(self._device, "get_both")
                _LOGGER.debug("Asked the robot to re-publish its map (dp 16)")
            except Exception as e:
                _LOGGER.debug("Could not request map upload: %s", e)

        self._last_fetch = 0.0
        await self.async_reload_map_list()
        await self._maybe_refresh()

    async def async_reload_map_list(self):
        """Reload the list of stored maps (multi-floor + saved sessions)."""
        if self._cloud is None:
            return []
        try:
            async with self._cloud:
                maps = await self._cloud.list_multi_maps()
                if not maps:
                    maps = await self._cloud.list_maps(file_type="pic", page_size=20)
        except Exception as e:
            self._last_error = f"map list: {e}"
            _LOGGER.debug("Could not list maps: %s", e)
            return []

        self._maps = [
            {
                "id": m.get("id"),
                "name": (m.get("file_name") or m.get("extend") or "").strip()
                or f"Map {m.get('id')}",
                "time": m.get("time"),
            }
            for m in maps
        ]
        _LOGGER.debug("%d stored map(s) found", len(self._maps))
        return self._maps

    async def async_select_map(self, map_id):
        """Choose which stored map to display. None = current/live map."""
        if map_id is not None:
            map_id = int(map_id)
            known = {m["id"] for m in self._maps}
            if known and map_id not in known:
                raise ValueError(
                    f"Unknown map id {map_id}. Available: {sorted(known)}"
                )
        self._selected_map_id = map_id
        self._last_fetch = 0.0
        # no device request here: we want the stored map, not a fresh upload
        await self._maybe_refresh()
        self.async_write_ha_state()

    async def _maybe_refresh(self):
        import time

        if self._cloud is None:
            return
        if time.time() - self._last_fetch < self.MIN_REFRESH_SECONDS:
            return
        self._last_fetch = time.time()

        if not self._maps:
            await self.async_reload_map_list()

        sel = self._selected_map_id
        try:
            async with self._cloud:
                raw_layout = await self._cloud.get_map_bytes(map_type=0, file_id=sel)
                if not raw_layout:
                    self._last_error = (
                        "no map returned by the cloud - the robot may not have "
                        "mapped yet, or Sweeping Robot Open Service may not be "
                        "authorised on the Tuya IoT project"
                    )
                    _LOGGER.warning("Map camera: %s", self._last_error)
                    return
                # the trajectory only makes sense for the live map
                raw_path = (
                    await self._cloud.get_map_bytes(map_type=1) if sel is None else None
                )
        except Exception as e:
            self._last_error = f"fetch: {e}"
            _LOGGER.warning("Map camera: could not fetch map: %s", e)
            return

        # Binary decoding is delegated to the tuya-vacuum library rather than
        # reimplemented, since the format moves with firmware versions.
        try:
            import io

            from tuya_vacuum.map import Map, Path

            from .map_decoder import decode_layout, image_format, unwrap_container

            unwrapped = unwrap_container(raw_layout)
            fmt = image_format(unwrapped)
            if fmt:
                # Déjà une image : on la sert telle quelle, sans décodage.
                self._png = unwrapped
                self._layout = None
                self._last_error = None
                _LOGGER.debug("Carte servie directement en %s", fmt)
                return

            # decode_layout picks the implementation matching the map version
            # found in the header (0 "normal", 1 "partition",
            # 2 "floor material"), so every version renders with the same
            # palette instead of depending on which decoder handled it.
            try:
                from .map_decoder import areas_from_archive

                self._map_areas = areas_from_archive(raw_layout)
            except Exception as e:
                _LOGGER.debug("Zones de l'archive non lues : %s", e)

            # Diagnostic des archives multi-fichiers (cartes enregistrées LDS)
            try:
                from .map_decoder import extract_tar, is_tar, unwrap_container

                probe = unwrap_container(raw_layout)
                if is_tar(probe):
                    members = extract_tar(probe)
                    self._archive_probe = {
                        name: (len(c), c[:16].hex(" "))
                        for name, c in members.items()
                    }
                    _LOGGER.info("Archive de carte : %s", self._archive_probe)
            except Exception as e:
                _LOGGER.debug("Sonde d'archive : %s", e)

            layout = decode_layout(raw_layout)
            path = Path(raw_path) if raw_path else None
            if raw_path:
                from .map_decoder import decode_path, path_from_archive

                try:
                    unwrapped_path = unwrap_container(raw_path)
                    from .map_decoder import is_tar

                    if is_tar(unwrapped_path):
                        self._cloud_path = path_from_archive(raw_path)
                    else:
                        self._cloud_path = decode_path(unwrapped_path)
                except Exception as e:
                    _LOGGER.debug("Trajectoire cloud non décodée : %s", e)

            image = None
            if path is not None:
                try:
                    image = Map(layout, path).to_image()
                except Exception as e:
                    # Losing the trajectory is far better than losing the map.
                    _LOGGER.debug("Could not overlay the path: %s", e)
            if image is None:
                image = layout.to_image()
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            self._png = buf.getvalue()
            self._layout = layout
            self._last_error = None
            _LOGGER.debug(
                "Map decoded: %sx%s, %d room(s)",
                getattr(layout, "width", "?"),
                getattr(layout, "height", "?"),
                len(getattr(layout, "rooms", []) or []),
            )
        except ImportError:
            self._last_error = "the 'tuya-vacuum' package is not installed"
            _LOGGER.error(
                "Map decoding unavailable: the 'tuya-vacuum' package is missing. "
                "It is declared in manifest.json; restart Home Assistant so it "
                "gets installed."
            )
        except NotImplementedError as e:
            self._last_error = f"unsupported map format: {e}"
            _LOGGER.warning(
                "Unsupported map format: %s (versions 0, 1 and 2 are handled; "
                "please report the version number)",
                e,
            )
        except Exception as e:
            self._last_error = f"decode: {e}"
            _LOGGER.warning("Map decoding failed: %s", e)

    @property
    def extra_state_attributes(self):
        """Expose map geometry so a Lovelace card can overlay rooms/zones."""
        attrs = dict(super().extra_state_attributes or {})
        attrs["maps"] = self._maps
        attrs["map_names"] = [m["name"] for m in self._maps]
        attrs["selected_map_id"] = self._selected_map_id
        map_areas = []
        for i, a in enumerate(self._map_areas or []):
            if ("map", i) in self._hidden_areas:
                continue
            item = dict(a)
            item["source"] = "map"
            item["index"] = i
            item["editable"] = True
            map_areas.append(item)
        attrs["areas"] = self._areas_payload() + self._stored_areas() + map_areas

        # Position du robot : dp 14 en local (temps réel) ; à défaut, dernier
        # point de la trajectoire cloud décodée au dernier téléchargement.
        path = self._robot_payload()
        source = "dp14"
        if not path.get("position") and self._cloud_path:
            path = self._cloud_path
            source = "cloud"
        if path.get("position"):
            attrs["robot_position"] = path["position"]
            attrs["robot_theta"] = path.get("theta")
            attrs["robot_source"] = source
            attrs["path"] = [
                [p["x"], p["y"]] for p in path.get("points", []) if p.get("visible")
            ]
        else:
            attrs["robot_source"] = "aucune"

        # Diagnostic : dire lequel des deux dp porte réellement des données,
        # pour identifier la source de position sur ce firmware précis.
        attrs["dp_diagnostics"] = self._dp_diagnostics()
        if self._archive_probe:
            attrs["archive_probe"] = self._archive_probe
        if self._last_error:
            attrs["last_error"] = self._last_error
        lay = self._layout
        if lay is None:
            return attrs
        attrs["map_state"] = {
            "version": getattr(lay, "version", None),
            "compressed_bytes": getattr(lay, "length_after_compression", None),
            "uncompressed_bytes": getattr(lay, "length_before_compression", None),
            "width": getattr(lay, "width", 0),
            "height": getattr(lay, "height", 0),
            "resolution": getattr(lay, "map_resolution", 0),
            "origin": [getattr(lay, "origin_x", 0), getattr(lay, "origin_y", 0)],
            "dock_px": [getattr(lay, "pile_x", 0), getattr(lay, "pile_y", 0)],
            "rooms": self._rooms_payload(lay),
        }
        ms = attrs["map_state"]

        # Statut réel du robot, pour ne pas le dire "docké" à tort.
        try:
            status = self._device.get_property("5")
        except Exception:
            status = None
        ms["robot_status"] = status
        really_docked = status in ("charging", "charge_done", "standby", "sleep")
        moving = status in self.ACTIVE_STATES

        if attrs.get("robot_position"):
            ms["robot_px"] = attrs["robot_position"]
            ms["robot_theta"] = attrs.get("robot_theta")
            ms["robot_docked"] = really_docked
            ms["robot_moving"] = moving
        elif ms.get("dock_px"):
            # Pas de position temps réel. On place l'icône sur le dock pour
            # qu'elle reste visible, mais on ne prétend "docké" que si le
            # statut le confirme : en goto_charge il se dirige vers le dock,
            # il n'y est pas encore.
            ms["robot_px"] = ms["dock_px"]
            ms["robot_theta"] = None
            ms["robot_docked"] = really_docked
            ms["robot_moving"] = moving
            ms["robot_position_known"] = False
        return attrs

    def _rooms_payload(self, layout):
        """Pièces avec leurs polygones, pour que la carte puisse les dessiner."""
        from .map_decoder import polygon_centroid, room_vertices

        rooms = []
        for r in getattr(layout, "rooms", []) or []:
            vertices = room_vertices(r)
            rooms.append(
                {
                    "id": getattr(r, "id", None),
                    "name": getattr(r, "name", ""),
                    "vertices": vertices,
                    "centroid": polygon_centroid(vertices),
                    "sweep_forbidden": bool(getattr(r, "sweep_forbidden", 0)),
                    "mop_forbidden": bool(getattr(r, "mop_forbidden", 0)),
                }
            )
        return rooms

    def _robot_payload(self):
        """Position et cap du robot, depuis le dp 14 (LOCAL, temps réel).

        Le dp 14 est poussé par le robot en continu pendant le nettoyage, donc
        le marqueur suit ses déplacements sans solliciter le cloud. Quand le
        robot est à l'arrêt, le dernier point reste sa position connue.
        """
        import base64

        from .map_decoder import decode_path

        try:
            raw = self._device.get_property("14")
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            data = base64.b64decode(raw)
        except Exception:
            return {}
        try:
            return decode_path(data)
        except Exception as e:
            _LOGGER.debug("Could not decode path: %s", e)
            return {}

    def remember_area(self, kind: str, points_list):
        """Mémorise une zone envoyée, pour pouvoir la réafficher.

        Le robot ne renvoie pas systématiquement les zones qu'on lui a posées
        sur le dp 15 ; sans mémorisation elles disparaîtraient de la carte
        après l'envoi.
        """
        self._zones.setdefault(kind, [])
        for points in points_list:
            self._zones[kind].append([list(p) for p in points])
        self.async_write_ha_state()
        self._persist_zones()

    def forget_areas(self, kind: str | None = None, index: int | None = None,
                     source: str = "stored"):
        """Oublie des zones : mémorisées (stored) ou issues de la carte (map)."""
        if source == "map":
            if index is None:
                # masquer toutes les zones de carte
                self._hidden_areas.update(("map", i) for i in range(len(self._map_areas)))
            else:
                self._hidden_areas.add(("map", index))
            self.async_write_ha_state()
            self._persist_zones()
            return
        if kind is None:
            self._zones = {}
        elif index is None:
            self._zones.pop(kind, None)
        else:
            zones = self._zones.get(kind) or []
            if 0 <= index < len(zones):
                zones.pop(index)
                self._zones[kind] = zones
            else:
                raise ValueError(
                    f"Index {index} hors limites pour {kind} "
                    f"({len(zones)} zone(s))"
                )
        self.async_write_ha_state()
        self._persist_zones()

    def _stored_areas(self):
        out = []
        for kind, zones in (self._zones or {}).items():
            for index, points in enumerate(zones):
                out.append(
                    {"type": kind, "points": points, "stored": True, "index": index}
                )
        return out

    def _persist_zones(self):
        """Sauvegarde les zones pour qu'elles survivent à un redémarrage."""
        try:
            from homeassistant.helpers.storage import Store

            store = Store(
                self._device._hass, 1, f"{DOMAIN}.{self.unique_id}.zones"
            )
            payload = {
                "zones": self._zones,
                "hidden": [list(h) for h in self._hidden_areas],
            }
            self._device._hass.async_create_task(store.async_save(payload))
        except Exception as e:
            _LOGGER.debug("Zones non persistées : %s", e)

    async def _restore_zones(self):
        try:
            from homeassistant.helpers.storage import Store

            store = Store(self._device._hass, 1, f"{DOMAIN}.{self.unique_id}.zones")
            data = await store.async_load()
            if data:
                # rétrocompat : ancien format = dict de zones seul
                if "zones" in data or "hidden" in data:
                    self._zones = data.get("zones", {})
                    self._hidden_areas = {tuple(h) for h in data.get("hidden", [])}
                else:
                    self._zones = data
                _LOGGER.debug("Zones restaurées")
        except Exception as e:
            _LOGGER.debug("Zones non restaurées : %s", e)

    def _dp_diagnostics(self):
        """Taille des dp 14 et 15 reçus, pour diagnostiquer la position."""
        import base64

        out = {}
        for dp, label in (("14", "path_data"), ("15", "command_trans")):
            try:
                raw = self._device.get_property(dp)
            except Exception:
                out[label] = "illisible"
                continue
            if not raw:
                out[label] = "vide"
                continue
            try:
                data = base64.b64decode(raw)
                out[label] = f"{len(data)} octets, début {data[:6].hex()}"
            except Exception:
                out[label] = f"non base64 ({type(raw).__name__})"
        return out

    def _areas_payload(self):
        """Zones interdites et murs déjà posés, décodés depuis le dp 15."""
        import base64

        from . import map_protocol as proto

        try:
            raw = self._device.get_property("15")
        except Exception:
            return []
        if not raw:
            return []
        try:
            data = base64.b64decode(raw)
        except Exception:
            return []
        try:
            return [
                {"type": a.cmd.name, "points": a.points}
                for a in proto.decode_areas(data)
            ]
        except Exception as e:
            _LOGGER.debug("Could not decode existing areas: %s", e)
            return []
