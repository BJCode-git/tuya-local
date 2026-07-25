# -*- coding: utf-8 -*-
"""
map_protocol.py — Portage fidèle du protocole binaire "Laser Robot Vacuum" de Tuya
==================================================================================

Source de vérité : tuya/tuya-panel-demo, examples/laserSweepRobot/src/protocol
  - protocol/area/index.ts        (no-go zones / murs virtuels / zones de nettoyage)
  - protocol/constant/index.ts    (couleurs, types, objets AI)
  - protocol/utils/pressCoordinateUtil/index.ts (transformXY, résolution 乐动/LDS)
  - protocol/utils/robotUtil/index.ts (toFixed16, décodage couleur)

CE MODULE EST DU PUR CALCUL — il est testable et testé hors-ligne (voir __main__).
Il fait ce que la lib tuya-vacuum NE fait pas : décoder ET encoder les "area"
(禁区/虚拟墙/划区), pour pouvoir AFFICHER *et* DÉSIGNER des no-go zones depuis HA.

Le D10 est un robot 乐动/LDS : coordonnées robot en millimètres, résolution de
map rapportée *100, offset (min_x/min_y) = origin de l'en-tête de map.

⚠ POINTS À VALIDER TERRAIN (marqués VALIDATION) :
  (V1) l'échelle exacte pixel<->monde dépend du firmware ; recaler une fois.
  (V2) l'encodage d'ENVOI (build_area_frame) est confirmé sur cousins LDS,
       à valider sur le D10 précis avant usage destructif.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# ---------------------------------------------------------------------------
#  Transforms de coordonnées (fidèles à pressCoordinateUtil)
# ---------------------------------------------------------------------------
_MAX = 16 ** 4          # 65536
_MIN = _MAX // 2        # 32768


def high_low_to_int(hi: int, lo: int) -> int:
    """2 octets big-endian -> entier 16 bits. (Utils.NumberUtils.highLowToInt)"""
    return (hi << 8) | lo


def deal_pl(value: int) -> int:
    """Interprète l'entier 16 bits en SIGNÉ (moitié haute = négatif)."""
    return value - _MAX if value > _MIN else value


def int_to_high_low(value: int) -> tuple[int, int]:
    """Inverse de high_low_to_int, gère le signe (pour ENVOYER)."""
    v = value & 0xFFFF
    return (v >> 8) & 0xFF, v & 0xFF


def scale_number(scale: int, value: float) -> float:
    """value / 10**scale, arrondi à `scale` décimales. (scaleNumber)"""
    return round(value / (10 ** scale), scale)


def transform_xy(map_scale: int, hi: int, lo: int) -> float:
    """2 octets -> coordonnée app signée mise à l'échelle. (transformXY)"""
    return scale_number(map_scale, deal_pl(high_low_to_int(hi, lo)))


# --- Famille 乐动 / LDS (le D10) : conversion via la résolution -------------
def robot_to_app(x_mm: float, y_mm: float, resolution: float) -> tuple[float, float]:
    """Coord robot (mm) -> coord app (pixels map). (transformXYByResolution)"""
    nx = round((x_mm * 0.001) / resolution, 2)
    ny = -round((y_mm * 0.001) / resolution, 2)
    return nx, ny


def app_to_robot(
    x_app: float, y_app: float, resolution: float, height: int, min_x: float, min_y: float
) -> tuple[int, int]:
    """Coord app (pixels) -> coord robot (mm). (transformXYToRobotByResolution)

    C'est CETTE fonction qui permet d'ENVOYER une no-go zone : l'utilisateur
    dessine en pixels sur la card, on convertit en mm pour le robot.
    """
    nx = round((x_app * resolution + min_x) / 0.001)
    ny = round(((height - y_app) * resolution + min_y) / 0.001)
    return int(nx), int(ny)


# ---------------------------------------------------------------------------
#  Opcodes "area" (protocol/area splitCommonData)  —  trame AA ver len cmd .. chk
# ---------------------------------------------------------------------------
class AreaCmd(Enum):
    """Opcodes de RÉCEPTION (device -> app), pour décoder command_trans.

    ATTENTION : les opcodes d'ENVOI sont DIFFÉRENTS (voir SendCmd).
    Le SDK utilise systématiquement N pour envoyer et N+1 pour recevoir.
    """
    SWEEP_REGION = 0x11        # 划区清扫 (zone de nettoyage)
    VIRTUAL_WALL = 0x13        # 虚拟墙 (mur virtuel) — 2 points = segment
    APPOINT = 0x17             # 定点 (point de nettoyage)
    VIRTUAL_AREA = 0x19        # 禁区 (NO-GO ZONE) — 4 points = quad
    VIRTUAL_AREA_V2 = 0x1B     # 禁区 V1.1.0 (sweep/mop séparés)
    SWEEP_REGION_V2 = 0x29     # 划区清扫 V1.1.0
    MATERIAL_OBJ = 0x37        # objets AI (fil, chaussure...)


class SendCmd(Enum):
    """Opcodes d'ENVOI (app -> device), depuis utils/index.tsx du SDK Tuya.

    Valeurs vérifiées dans le code officiel :
        areaSetType   = '10'   划区清扫
        areaSetTypeV2 = '28'   划区清扫 1.1.0
        vrWallType    = '12'   虚拟墙
        pressToRunType= '16'   指哪扫哪
        vrAreaType    = '18'   禁区设置
        vrAreaTypeV2  = '1a'   禁区设置 1.1.0
        roomClean     = '14'   (encodeRoomClean)
    """
    ZONE_CLEAN = 0x10
    VIRTUAL_WALL = 0x12
    ROOM_CLEAN = 0x14
    GO_TO_POINT = 0x16
    NO_GO_ZONE = 0x18
    NO_GO_ZONE_V2 = 0x1A
    ZONE_CLEAN_V2 = 0x28


class ForbidType(str, Enum):
    ALL = "all"
    SWEEP = "sweep"
    MOP = "mop"


# points par item selon le type (chunk du protocole)
_POINTS_PER_ITEM = {
    AreaCmd.APPOINT: 1,        # chunk 4 octets  -> 1 point
    AreaCmd.VIRTUAL_WALL: 2,   # chunk 8 octets  -> 2 points
    AreaCmd.VIRTUAL_AREA: 4,   # chunk 16 octets -> 4 points
    AreaCmd.SWEEP_REGION: 4,
}


@dataclass
class Area:
    """Une zone décodée/à encoder, en coordonnées app (pixels map)."""
    cmd: AreaCmd
    points: list[tuple[float, float]]
    forbid_type: ForbidType = ForbidType.ALL
    area_id: int | None = None


# ---------------------------------------------------------------------------
#  DÉCODAGE (device -> app) : lit les frames "area" reçues sur command_trans
# ---------------------------------------------------------------------------
def _checksum(frame_wo_header_and_check: bytes) -> int:
    """XOR de tous les octets sauf le header 0xAA (comme clean_rooms/helper)."""
    c = 0
    for b in frame_wo_header_and_check:
        c ^= b
    return c


def split_frames(data: bytes) -> list[tuple[int, bytes]]:
    """Découpe un flux command_trans en frames (cmd, payload). (splitCommonData)

    Frame = AA | version | length | cmd | data(length octets...) | (check).
    On tolère plusieurs frames concaténées.
    """
    out: list[tuple[int, bytes]] = []
    i = 0
    n = len(data)
    while i < n:
        if data[i] != 0xAA:
            i += 1
            continue
        if i + 3 >= n:
            break
        length = data[i + 2]
        cmd = data[i + 3]
        start = i + 4
        end = start + max(length - 1, 0)   # length couvre cmd+data selon le SDK
        payload = data[start:end]
        out.append((cmd, payload))
        i = end + 1                          # saute l'octet de check
    return out


def decode_areas(data: bytes, map_scale: int = 1) -> list[Area]:
    """Décode toutes les zones (no-go, murs, zones) d'un flux command_trans."""
    areas: list[Area] = []
    for cmd_int, payload in split_frames(data):
        try:
            cmd = AreaCmd(cmd_int)
        except ValueError:
            continue
        if cmd not in _POINTS_PER_ITEM:
            continue
        pts_per = _POINTS_PER_ITEM[cmd]
        bytes_per_item = pts_per * 4
        for off in range(0, len(payload) - bytes_per_item + 1, bytes_per_item):
            item = payload[off:off + bytes_per_item]
            points = []
            for p in range(0, len(item), 4):
                x = transform_xy(map_scale, item[p], item[p + 1])
                y = -transform_xy(map_scale, item[p + 2], item[p + 3])
                points.append((x, y))
            areas.append(Area(cmd=cmd, points=points))
    return areas


# ---------------------------------------------------------------------------
#  ENCODAGE (app -> device) — portage fidèle de utils/index.tsx du SDK Tuya
# ---------------------------------------------------------------------------
#  Trame : AA | version(00) | length | cmd | data | checksum
#      length   = 1 (cmd) + len(data)
#      checksum = (cmd + somme des octets de data) & 0xFF   <-- SOMME, pas XOR
#
#  Vérifié sur la trame de référence du SDK (commentaire de getCommonData) :
#      aa 12 10 01 fd8a02aa ffe202aa ffe20052 fd8a0052 dd
#      0x10 + 1 + somme(coordonnées) = 2269 ; 2269 % 256 = 0xdd  ✓
#
#  Les coordonnées ne sont PAS en millimètres : elles sont relatives à
#  l'origine de la carte, modulo 65535, avec l'axe Y inversé :
#      x = (65535 + x*10^scale - origin_x*10^scale) % 65535
#      y = (65535 + origin_y*10^scale - y*10^scale) % 65535
# ---------------------------------------------------------------------------
FRAME_HEADER = 0xAA
FRAME_VERSION = 0x00
MAP_HEADER = 0xAB          # trames de gestion multi-cartes


def _deal_scale(value: float, scale: int) -> int:
    """dealScale du SDK : arrondi à `scale` décimales puis mise à l'échelle."""
    return int(round(round(float(value), scale) * (10 ** scale)))


def app_to_frame(
    x: float,
    y: float,
    origin_x: float,
    origin_y: float,
    map_scale: int = 1,
) -> tuple[int, int]:
    """Coordonnée de carte -> couple d'entiers 16 bits de la trame."""
    ox = origin_x * (10 ** map_scale)
    oy = origin_y * (10 ** map_scale)
    fx = int((65535 + _deal_scale(x, map_scale) - ox) % 65535)
    fy = int((65535 + oy - _deal_scale(y, map_scale)) % 65535)
    return fx, fy


def _points_bytes(points, origin, map_scale: int = 1) -> bytes:
    out = bytearray()
    ox, oy = origin
    for (x, y) in points:
        fx, fy = app_to_frame(x, y, ox, oy, map_scale)
        out += fx.to_bytes(2, "big") + fy.to_bytes(2, "big")
    return bytes(out)


def build_frame(cmd: int, data: bytes) -> bytes:
    """Assemble une trame command_trans complète (checksum en somme)."""
    length = len(data) + 1
    if length > 0xFF:
        raise ValueError(f"Trame trop longue : {length} octets")
    checksum = (cmd + sum(data)) & 0xFF
    return bytes([FRAME_HEADER, FRAME_VERSION, length, cmd]) + data + bytes([checksum])


# --- zones interdites -------------------------------------------------------
def encode_no_go_zones(
    zones,
    origin=(0.0, 0.0),
    map_scale: int = 1,
    forbid_type: str | None = None,
) -> bytes:
    """Zones interdites (禁区). 4 points par zone.

    forbid_type None  -> format V1 (cmd 0x18), interdit total
    forbid_type set   -> format V2 (cmd 0x1a), avec un octet de mode par zone
                         ('sweep' -> 0x00, sinon 0x02)
    """
    if forbid_type is None:
        data = bytearray([len(zones)])
        for zone in zones:
            data += _points_bytes(zone, origin, map_scale)
        return build_frame(SendCmd.NO_GO_ZONE.value, bytes(data))

    mode = 0x00 if str(forbid_type) == "sweep" else 0x02
    data = bytearray([len(zones)])
    for zone in zones:
        data.append(mode)
        data.append(len(zone))
        data += _points_bytes(zone, origin, map_scale)
    return build_frame(SendCmd.NO_GO_ZONE_V2.value, bytes(data))


# --- murs virtuels ----------------------------------------------------------
def encode_virtual_walls(walls, origin=(0.0, 0.0), map_scale: int = 1) -> bytes:
    """Murs virtuels (虚拟墙). 2 points par mur."""
    data = bytearray([len(walls)])
    for wall in walls:
        data += _points_bytes(wall, origin, map_scale)
    return build_frame(SendCmd.VIRTUAL_WALL.value, bytes(data))


# --- zones de nettoyage -----------------------------------------------------
def encode_zone_clean(
    zones,
    origin=(0.0, 0.0),
    map_scale: int = 1,
    passes: int | None = None,
) -> bytes:
    """Nettoyage de zones (划区清扫). 4 points par zone.

    passes fourni -> format V2 (cmd 0x28) avec compteur de passages.
    """
    if passes is None:
        data = bytearray([len(zones)])
        for zone in zones:
            data += _points_bytes(zone, origin, map_scale)
        return build_frame(SendCmd.ZONE_CLEAN.value, bytes(data))

    data = bytearray([int(passes) & 0xFF, len(zones)])
    for zone in zones:
        data.append(len(zone))
        data += _points_bytes(zone, origin, map_scale)
    return build_frame(SendCmd.ZONE_CLEAN_V2.value, bytes(data))


# --- point de nettoyage -----------------------------------------------------
def encode_go_to_point(point, origin=(0.0, 0.0), map_scale: int = 1) -> bytes:
    """Envoie le robot à un point (指哪扫哪)."""
    return build_frame(
        SendCmd.GO_TO_POINT.value, _points_bytes([point], origin, map_scale)
    )


# --- nettoyage par pièce ----------------------------------------------------
def encode_room_clean(room_ids, clean_count: int = 1) -> bytes:
    """Nettoyage par pièce (encodeRoomClean du SDK).

    Ordre des champs : cmd | clean_count | nb_pièces | ids...
    """
    ids = [int(r) & 0xFF for r in room_ids]
    data = bytes([int(clean_count) & 0xFF, len(ids)]) + bytes(ids)
    return build_frame(SendCmd.ROOM_CLEAN.value, data)


# ---------------------------------------------------------------------------
#  Multi-cartes — trames 0xAB (longueur sur 4 octets, checksum en somme)
# ---------------------------------------------------------------------------
def _build_map_frame(cmd: int, data: bytes) -> bytes:
    length = len(data) + 1
    checksum = (cmd + sum(data)) & 0xFF
    return (
        bytes([MAP_HEADER, FRAME_VERSION])
        + length.to_bytes(4, "big")
        + bytes([cmd])
        + data
        + bytes([checksum])
    )


def encode_save_map() -> bytes:
    """Enregistre la carte courante comme nouvelle carte (多地图管理-保存地图).

    Le SDK code cette trame en dur : ab00000000022a012b
    """
    return _build_map_frame(0x2A, bytes([0x01]))


def encode_delete_map(map_id: int) -> bytes:
    """Supprime une carte enregistrée, côté robot (cmd 0x2c)."""
    return _build_map_frame(0x2C, int(map_id).to_bytes(4, "big"))


def encode_use_map(map_id: int, url: str) -> bytes:
    """Active une carte enregistrée (cmd 0x2e).

    L'URL est celle du fichier de carte fournie par le cloud : le robot va la
    télécharger lui-même. C'est le mécanisme de bascule multi-étages de l'app.
    """
    url_bytes = url.encode("utf-8")
    data = (
        int(map_id).to_bytes(4, "big")
        + len(url_bytes).to_bytes(4, "big")
        + url_bytes
    )
    return _build_map_frame(0x2E, data)


# ---------------------------------------------------------------------------
#  Couleurs (fidèles à constant/index.ts) — pour la card front-end
# ---------------------------------------------------------------------------
# Palette des pièces (16 couleurs, cycle par room.id)
ROOM_COLORS = [
    "#F9424F", "#FDD02B", "#46A890", "#208CFF", "#FA7A80", "#A8E772",
    "#49F9CA", "#7CF8FF", "#F3A0A4", "#79E420", "#65EAF3", "#A188F8",
    "#F8C6A1", "#BCFF83", "#5CDBFA", "#EEBAFC",
]
# Points de base : fond / obstacle / inconnu / dock
BG_COLOR = "#ABD6FF"        #清扫点 (fond de map)
OBSTACLE_COLOR = "#818181"  # 障碍点 (murs/bordures)
DOCK_COLOR = "#7ED321"      # 充电桩 (base de charge)
UNKNOWN_COLOR = "#00000000"

# Objets AI détectés (materialObjMap)
MATERIAL_OBJ = {
    0: "wire", 1: "shoe", 2: "sock", 3: "toy",
    4: "chair", 5: "table", 6: "ashcan", 7: "plants",
}

# ---------------------------------------------------------------------------
#  Codes de retour / statut / défauts (doc Tuya + data model D10)
# ---------------------------------------------------------------------------
# status (DP5) -> état HA
STATUS_MAP = {
    "standby": "idle", "sleep": "idle", "smart": "cleaning",
    "zone_clean": "cleaning", "part_clean": "cleaning", "cleaning": "cleaning",
    "select_room": "cleaning", "goto_pos": "cleaning", "pos_arrived": "cleaning",
    "paused": "paused", "pos_unarrive": "error",
    "goto_charge": "returning", "charging": "docked", "charge_done": "docked",
    "in_trouble": "error",
}

# fault (DP28) — bitmap ; libellés du data model D10 (30 bits)
FAULT_BITS = {
    0: "edge_sweep", 1: "middle_sweep", 2: "left_wheel", 3: "right_wheel",
    4: "garbage_box", 5: "land_check", 6: "collision", 7: "stuck",
    8: "fan", 9: "dustbin_full", 10: "water_box", 11: "mop_missing",
    12: "low_battery", 13: "lidar_blocked", 14: "wheel_hang", 15: "cliff",
}


def decode_faults(fault_value: int, table: dict[int, str] = FAULT_BITS) -> list[str]:
    """Bitmap de défauts -> liste de libellés actifs."""
    return [name for bit, name in table.items() if fault_value & (1 << bit)]


# ===========================================================================
#  Tests hors-ligne
# ===========================================================================
if __name__ == "__main__":
    # ---- transforms signées (décodage) -------------------------------------
    for v in (0, 1, 100, 32767, -1, -100, -32767):
        hi, lo = int_to_high_low(v)
        assert deal_pl(high_low_to_int(hi, lo)) == v, v
    assert transform_xy(0, 0x80, 0x01) == -32767
    assert transform_xy(0, 0x00, 0x64) == 100

    # ---- CHECKSUM : validé sur la trame de référence du SDK -----------------
    ref = bytes.fromhex("aa121001fd8a02aaffe202aaffe20052fd8a0052dd")
    assert (ref[2] + ref[3] + sum(ref[4:-1])) % 256 == ref[-1]

    # ---- reproduction exacte de cette trame ---------------------------------
    # Le SDK omet l'octet de version dans son commentaire ; on reconstruit le
    # corps (cmd + data) et on vérifie longueur et checksum.
    body = ref[2:-1]
    frame = build_frame(body[0], body[2:] if False else body[1:])
    assert frame[3] == 0x10                       # cmd areaSetType
    assert frame[2] == len(body)                  # length = cmd + data
    assert frame[-1] == ref[-1], (hex(frame[-1]), hex(ref[-1]))
    print("frame reconstruite :", frame.hex())

    # ---- opcodes d'envoi (SDK) ---------------------------------------------
    assert SendCmd.ZONE_CLEAN.value == 0x10
    assert SendCmd.VIRTUAL_WALL.value == 0x12
    assert SendCmd.ROOM_CLEAN.value == 0x14
    assert SendCmd.GO_TO_POINT.value == 0x16
    assert SendCmd.NO_GO_ZONE.value == 0x18
    assert SendCmd.NO_GO_ZONE_V2.value == 0x1A
    assert SendCmd.ZONE_CLEAN_V2.value == 0x28
    # envoi = réception - 1
    assert SendCmd.NO_GO_ZONE.value == AreaCmd.VIRTUAL_AREA.value - 1
    assert SendCmd.VIRTUAL_WALL.value == AreaCmd.VIRTUAL_WALL.value - 1

    # ---- coordonnées relatives à l'origine ---------------------------------
    assert app_to_frame(0, 0, 0, 0, 1) == (0, 0)
    fx, fy = app_to_frame(-1.0, 2.0, 0.0, 0.0, 1)
    assert fx == 65525 and fy == 65515, (fx, fy)   # négatifs -> modulo

    # ---- no-go zone V1 ------------------------------------------------------
    z = [[(1, 1), (3, 1), (3, 3), (1, 3)]]
    f = encode_no_go_zones(z, origin=(0, 0))
    assert f[0] == 0xAA and f[3] == 0x18 and f[4] == 1
    assert f[-1] == (f[3] + sum(f[4:-1])) & 0xFF
    assert len(f) == 4 + 1 + 16 + 1

    # ---- no-go zone V2 (mode sweep/mop) ------------------------------------
    fv2 = encode_no_go_zones(z, origin=(0, 0), forbid_type="sweep")
    assert fv2[3] == 0x1A and fv2[5] == 0x00 and fv2[6] == 4
    fv2m = encode_no_go_zones(z, origin=(0, 0), forbid_type="all")
    assert fv2m[5] == 0x02

    # ---- mur virtuel --------------------------------------------------------
    w = encode_virtual_walls([[(0, 0), (5, 0)]], origin=(0, 0))
    assert w[3] == 0x12 and w[4] == 1 and len(w) == 4 + 1 + 8 + 1

    # ---- nettoyage par pièce (ordre count/ids du SDK) ----------------------
    rc = encode_room_clean([3, 5], clean_count=2)
    assert rc[3] == 0x14 and rc[4] == 2 and rc[5] == 2 and rc[6:8] == bytes([3, 5])
    assert rc[-1] == (0x14 + 2 + 2 + 3 + 5) & 0xFF

    # ---- multi-cartes -------------------------------------------------------
    assert encode_save_map().hex() == "ab00000000022a012b", encode_save_map().hex()
    d = encode_delete_map(14272580)
    assert d[0] == 0xAB and d[6] == 0x2C and int.from_bytes(d[2:6], "big") == 5
    u = encode_use_map(7, "http://x/y.bin")
    assert u[6] == 0x2E and int.from_bytes(u[2:6], "big") == len("http://x/y.bin") + 9

    # ---- décodage (inchangé) ------------------------------------------------
    raw = bytes([0xAA, 0x00, 4 * 4 + 1, 0x19,
                 0x00, 0x0A, 0x00, 0x0A, 0x00, 0x1E, 0x00, 0x0A,
                 0x00, 0x1E, 0x00, 0x1E, 0x00, 0x0A, 0x00, 0x1E, 0x00])
    areas = decode_areas(raw, map_scale=0)
    assert len(areas) == 1 and areas[0].cmd == AreaCmd.VIRTUAL_AREA

    assert set(decode_faults(0b1001)) == {"edge_sweep", "right_wheel"}
    print("map_protocol: ALL TESTS PASSED")
