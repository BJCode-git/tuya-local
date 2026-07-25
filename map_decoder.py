"""
map_decoder.py - Layout decoding for every known Tuya LDS map version.

Ported from Tuya's own panel SDK:
tuya-panel-demo/examples/laserSweepRobot/src/protocol/map/index.ts

Why this module exists
----------------------
`tuya-vacuum` decodes versions 0 and 1 but raises NotImplementedError on
version 2, which recent LDS firmwares produce. Rather than add only the
missing version and delegate the rest, all three are implemented here, for
three concrete reasons:

  * consistent rendering - the library paints with its own hardcoded palette,
    so a map would change appearance depending on which version the firmware
    happened to emit. Here every version uses the vendor app palette.
  * consistent room handling - version 0 has no room block at all, and mixing
    two implementations leaves attributes in inconsistent states.
  * no private API - the header layout is parsed here rather than by calling
    the library's private `_parse_header`, which can change without notice.

`tuya-vacuum` is still used for the two things it does well and that are not
worth reimplementing: LZ4 decompression and room block parsing. It also
remains the fallback if decoding here fails.

The three formats
-----------------
Header (24 bytes, big-endian, identical across versions):

    offset 0     version
    offset 1-2   map id
    offset 3     type (0 = layout, 1 = path)
    offset 4-5   width          offset 6-7    height
    offset 8-9   origin x       offset 10-11  origin y
    offset 12-13 resolution     offset 14-15  dock x
    offset 16-17 dock y         offset 18-21  length BEFORE compression
    offset 22-23 length AFTER compression (0 when not compressed)

Both lengths matter. The SDK calls them compressBeforeLength (which it reads
from the same field as totalCount) and compressAfterLength, and it bounds
decompression with them. `tuya-vacuum`'s LZ4 routine has no output bound and
simply runs until the end of its input, so feeding it the whole remainder of
the file makes it walk into the padding that follows the LZ4 block, read a
zero token, and fail with "offset can't be 0". Both bounds are applied here.

Payload:

    version 0 "normal"           each byte packs FOUR 2-bit pixels, read from
                                 the most significant pair first. No rooms.
                                 Types: 00 floor, 01 obstacle,
                                        10 charging dock, 11 unknown.

    version 1 "partition"        one byte per pixel:
                                 bits 0-1 type, bits 2-7 room id (max 63).
                                 Same four types as version 0.
                                 Followed by the room block.

    version 2 "floor material"   one byte per pixel:
                                 bits 0-2 type, bits 3-7 room id (max 31).
                                 Types: 000 unknown, 001 obstacle,
                                        010 carpet, 111 floor.
                                 Room block identical to version 1.

Version 2 exists because carpet detection needed a point type of its own,
which did not fit in the two bits version 1 used - hence three bits for the
type and one fewer for the room id.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

HEADER_BYTES = 24

# --- point types -----------------------------------------------------------
# versions 0 and 1 (2 bits)
V1_FLOOR = 0b00
V1_OBSTACLE = 0b01
V1_DOCK = 0b10
V1_UNKNOWN = 0b11

# version 2 (3 bits)
V2_UNKNOWN = 0b000
V2_OBSTACLE = 0b001
V2_CARPET = 0b010
V2_FLOOR = 0b111

# --- palette (vendor app colours) -----------------------------------------
ROOM_COLORS = [
    "#F9424F", "#FDD02B", "#46A890", "#208CFF", "#FA7A80", "#A8E772",
    "#49F9CA", "#7CF8FF", "#F3A0A4", "#79E420", "#65EAF3", "#A188F8",
    "#F8C6A1", "#BCFF83", "#5CDBFA", "#EEBAFC",
]
# Palette calquée sur l'app Tuya : fond bleu clair, murs gris doux, sols pastel
BG_COLOR = (171, 214, 255)      # zone inconnue = fond de carte
OBSTACLE_COLOR = (129, 129, 129)  # 障碍点, gris de l'app
CARPET_COLOR = (196, 205, 218)  # tapis
FLOOR_COLOR = (233, 241, 252)   # sol nettoyé sans pièce identifiée
DOCK_COLOR = (126, 211, 33)     # 充电桩

# Les couleurs de pièce de l'app sont vives : on les éclaircit pour le fond
# de carte, comme le fait l'app, et on garde la teinte pure pour les contours.
ROOM_TINT = 0.55                # 0 = couleur pure, 1 = blanc


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _room_color(room_id: int) -> tuple[int, int, int]:
    """Couleur de pièce, éclaircie pour servir de fond lisible."""
    r, g, b = _rgb(ROOM_COLORS[room_id % len(ROOM_COLORS)])
    t = ROOM_TINT
    return (
        int(r + (255 - r) * t),
        int(g + (255 - g) * t),
        int(b + (255 - b) * t),
    )


# ---------------------------------------------------------------------------
#  Pixel decoding - pure functions, unit tested
# ---------------------------------------------------------------------------
def decode_pixel_v1(byte: int) -> tuple[int, int]:
    """Version 1 pixel -> (point type, room id). 2 bits type, 6 bits room."""
    return byte & 0b11, byte >> 2


def decode_pixel_v2(byte: int) -> tuple[int, int]:
    """Version 2 pixel -> (point type, room id). 3 bits type, 5 bits room."""
    return byte & 0b111, byte >> 3


def unpack_pixels_v0(data: bytes) -> bytearray:
    """Version 0: expand each byte into its four 2-bit point types.

    Pairs are read most significant first, matching the SDK.
    """
    out = bytearray()
    for byte in data:
        out.append((byte >> 6) & 0b11)
        out.append((byte >> 4) & 0b11)
        out.append((byte >> 2) & 0b11)
        out.append(byte & 0b11)
    return out


def _color_for_type_v1(point_type: int):
    if point_type == V1_OBSTACLE:
        return OBSTACLE_COLOR
    if point_type == V1_DOCK:
        return DOCK_COLOR
    if point_type == V1_UNKNOWN:
        return BG_COLOR
    return None  # floor: the caller decides, based on the room id


def pixel_color_v0(point_type: int):
    """Version 0 has no rooms, so cleaned floor is drawn plain."""
    colour = _color_for_type_v1(point_type)
    return FLOOR_COLOR if colour is None else colour


def pixel_color_v1(byte: int, room_ids: set[int] | None = None):
    point_type, room_id = decode_pixel_v1(byte)
    colour = _color_for_type_v1(point_type)
    if colour is not None:
        return colour
    if room_ids is not None and room_id not in room_ids:
        return FLOOR_COLOR
    return _room_color(room_id)


def pixel_color_v2(byte: int, room_ids: set[int] | None = None):
    point_type, room_id = decode_pixel_v2(byte)
    if point_type == V2_OBSTACLE:
        return OBSTACLE_COLOR
    if point_type == V2_CARPET:
        return CARPET_COLOR
    if point_type == V2_UNKNOWN:
        return BG_COLOR
    if point_type == V2_FLOOR:
        if room_ids is not None and room_id not in room_ids:
            return FLOOR_COLOR
        return _room_color(room_id)
    # Not defined by the SDK: draw as background rather than garbage.
    return BG_COLOR



# ---------------------------------------------------------------------------
#  Bounded LZ4 block decompression
# ---------------------------------------------------------------------------
def uncompress_bounded(src: bytes, max_output: int | None = None) -> bytes:
    """Decompress an LZ4 block, stopping once max_output bytes are produced.

    Tuya's own SDK decompresses with a maxBufferLength and stops there. The
    LZ4 routine bundled with `tuya-vacuum` has no such bound and keeps parsing
    until its input runs out, so any padding after the block is interpreted as
    LZ4 and typically fails with "offset can't be 0". Stopping at the known
    uncompressed length avoids that entirely.

    Follows the LZ4 block format: token byte (high nibble literal length, low
    nibble match length), literals, 2-byte little-endian match offset, then the
    match. The last sequence ends after its literals.
    """
    dst = bytearray()
    pos = 0
    end = len(src)
    MIN_MATCH = 4

    def read_length(value: int) -> int:
        nonlocal pos
        if value != 0x0F:
            return value
        while True:
            if pos >= end:
                raise ValueError("truncated length field")
            part = src[pos]
            pos += 1
            value += part
            if part != 0xFF:
                return value

    while pos < end:
        if max_output is not None and len(dst) >= max_output:
            break

        token = src[pos]
        pos += 1

        literal_len = read_length((token >> 4) & 0x0F)
        if literal_len:
            if pos + literal_len > end:
                raise ValueError("truncated literals")
            dst += src[pos : pos + literal_len]
            pos += literal_len

        # Enough output already: the rest of the block is not needed.
        if max_output is not None and len(dst) >= max_output:
            break

        # Final sequence: literals with no match.
        if pos + 2 > end:
            break

        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0:
            raise ValueError("offset can't be 0")
        if offset > len(dst):
            raise ValueError(f"offset {offset} beyond {len(dst)} decoded bytes")

        match_len = read_length(token & 0x0F) + MIN_MATCH
        for _ in range(match_len):
            dst.append(dst[-offset])

    if max_output is not None and len(dst) > max_output:
        del dst[max_output:]
    return bytes(dst)

# ---------------------------------------------------------------------------
#  Layout
# ---------------------------------------------------------------------------
class BaseLayout:
    """Common header parsing and rendering for all map versions."""

    version = -1

    def __init__(self, data: bytes) -> None:
        if len(data) < HEADER_BYTES:
            raise ValueError(
                f"Map too short: {len(data)} bytes, the header alone "
                f"needs {HEADER_BYTES}"
            )
        self.raw = data
        self._parse_header(data[:HEADER_BYTES])
        self.rooms = []
        self._pixels = bytearray()
        self._parse_payload(data)

    # -- header ------------------------------------------------------------
    def _parse_header(self, head: bytes) -> None:
        def u16(offset: int) -> int:
            return (head[offset] << 8) | head[offset + 1]

        self.version = head[0]
        self.id = u16(1)
        self.type = head[3]
        self.width = u16(4)
        self.height = u16(6)
        # Origine et position du dock : shrinkValue, soit /10 (SDK).
        self.origin_x = round(u16(8) / 10, 1)
        self.origin_y = round(u16(10) / 10, 1)
        self.pile_x = round(u16(14) / 10, 1)
        self.pile_y = round(u16(16) / 10, 1)
        # ATTENTION : la résolution n'est PAS traitée par shrinkValue dans le
        # SDK, qui précise « 地图分辨率，原始值*100上报 » — elle est rapportée
        # multipliée par 100, pas par 10.
        self.map_resolution_raw = u16(12)
        self.map_resolution = round(u16(12) / 100, 3)
        # The SDK reads compressBeforeLength from this same field.
        self.total_count = int.from_bytes(head[18:22], "big")
        self.length_before_compression = self.total_count
        self.length_after_compression = u16(22)
        self.room_editable = bool(self.type)

        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Invalid map size: {self.width}x{self.height}")

    # -- payload -----------------------------------------------------------
    def _decompress(self, data: bytes) -> bytes:
        payload = data[HEADER_BYTES:]

        if not self.length_after_compression:
            # Uncompressed payload: take it as is.
            return payload

        # Bound the INPUT to the compressed block. Anything after it is
        # padding and would be parsed as LZ4 by an unbounded decompressor.
        compressed = payload
        size = self.length_after_compression
        if 0 < size <= len(payload):
            compressed = payload[:size]
        else:
            _LOGGER.debug(
                "Compressed length %d is not usable for a %d byte payload, "
                "decompressing the whole remainder",
                size,
                len(payload),
            )

        # Bound the OUTPUT to the uncompressed length, like the SDK does with
        # maxBufferLength. This is what stops decoding at the right place even
        # if the input bound above was wrong.
        expected = self.length_before_compression or None
        try:
            return uncompress_bounded(compressed, expected)
        except Exception as e:
            _LOGGER.debug("Bounded decompression failed (%s), retrying unbounded", e)
            from tuya_vacuum.lz4 import uncompress

            return bytes(uncompress(compressed))

    def _parse_payload(self, data: bytes) -> None:
        raise NotImplementedError

    def _parse_rooms(self, block: bytes) -> None:
        from tuya_vacuum.map.room import Room

        if not block:
            self.rooms = []
            return
        try:
            self.rooms = Room.parse_rooms(block.hex())
        except Exception as e:
            # A map without a readable room block is still worth showing.
            _LOGGER.warning("Could not parse room block: %s", e)
            self.rooms = []

    # -- rendering ---------------------------------------------------------
    def _pixel_color(self, value: int, room_ids):
        raise NotImplementedError

    def to_image(self):
        import numpy as np
        from PIL import Image

        room_ids = {r.id for r in self.rooms} if self.rooms else None
        cache: dict[int, tuple[int, int, int]] = {}
        pixels = self._pixels
        width, height = self.width, self.height
        expected = width * height

        if len(pixels) < expected:
            _LOGGER.warning(
                "Map payload shorter than expected (%d of %d pixels), padding",
                len(pixels),
                expected,
            )
            pixels = pixels + bytearray(expected - len(pixels))

        rows = []
        for y in range(height):
            offset = y * width
            row = []
            for x in range(width):
                value = pixels[offset + x]
                colour = cache.get(value)
                if colour is None:
                    colour = self._pixel_color(value, room_ids)
                    cache[value] = colour
                row.append(colour)
            rows.append(row)

        image = Image.fromarray(np.array(rows, dtype=np.uint8))
        return self._upscale(image)

    # Une carte fait typiquement 100 à 400 px de côté : affichée en pleine
    # largeur, elle est floue ou crénelée. On l'agrandit en NEAREST pour garder
    # des bords nets, ce que fait aussi l'app.
    UPSCALE_TARGET = 800
    MAX_UPSCALE = 6

    def _upscale(self, image):
        from PIL import Image as PILImage

        w, h = image.size
        if not w or not h:
            return image
        factor = max(1, min(self.MAX_UPSCALE, round(self.UPSCALE_TARGET / max(w, h))))
        if factor <= 1:
            return image
        return image.resize((w * factor, h * factor), PILImage.NEAREST)

    def __repr__(self) -> str:
        return (
            f"<Layout v{self.version} {self.width}x{self.height} "
            f"rooms={len(self.rooms)}>"
        )


class LayoutV0(BaseLayout):
    """Version 0, "normal": four 2-bit pixels per byte, no rooms."""

    def _parse_payload(self, data: bytes) -> None:
        decoded = self._decompress(data)
        self._pixels = unpack_pixels_v0(decoded)[: self.width * self.height]
        self.rooms = []

    def _pixel_color(self, value: int, room_ids):
        return pixel_color_v0(value)


class LayoutV1(BaseLayout):
    """Version 1, "partition": one byte per pixel, 2 bits type / 6 bits room."""

    def _parse_payload(self, data: bytes) -> None:
        decoded = self._decompress(data)
        area = self.width * self.height
        self._pixels = bytearray(decoded[:area])
        self._parse_rooms(decoded[area:])

    def _pixel_color(self, value: int, room_ids):
        return pixel_color_v1(value, room_ids)


class LayoutV2(BaseLayout):
    """Version 2, "floor material": 3 bits type / 5 bits room, plus carpet."""

    def _parse_payload(self, data: bytes) -> None:
        decoded = self._decompress(data)
        area = self.width * self.height
        self._pixels = bytearray(decoded[:area])
        self._parse_rooms(decoded[area:])

    def _pixel_color(self, value: int, room_ids):
        return pixel_color_v2(value, room_ids)


# ---------------------------------------------------------------------------
#  Trajectoire et position du robot (path)
# ---------------------------------------------------------------------------
#  Porté de protocol/path/index.ts + robotUtil.createFormatPath du SDK.
#
#  En-tête, 13 octets :
#      0      version
#      1-2    path id
#      3      force update
#      4      type
#      5-8    nombre total de points
#      9-10   theta  <- CAP DU ROBOT, en dixièmes de degré
#      11-12  longueur après compression (0 = non compressé)
#
#  Chaque point fait 4 octets : x sur 2, y sur 2, en SIGNÉ 16 bits.
#  Les valeurs sont divisées par 10, et l'axe Y est inversé (reverseY).
#
#  Subtilité : le BIT DE POIDS FAIBLE de x et de y sert de drapeau. Si les
#  deux valent 0, le segment est un déplacement de nettoyage ; sinon c'est un
#  retour à la base, que l'app masque. On conserve l'information.
PATH_HEADER_BYTES = 13


def _signed16(value: int) -> int:
    return value - 65536 if value > 32768 else value


def path_from_archive(data: bytes) -> dict:
    """Trajectoire lue dans path_record / path_map d'une archive enregistrée."""
    raw = unwrap_container(data)
    if not is_tar(raw):
        return {}
    members = extract_tar(raw)
    for name in ("path_record", "path_map", "path"):
        if name in members and members[name]:
            try:
                return decode_path(members[name])
            except Exception as e:
                _LOGGER.debug("Path d'archive %s illisible : %s", name, e)
    return {}


def decode_path(data: bytes) -> dict:
    """Décode une trajectoire (dp 14 en local, ou rou.bin côté cloud)."""
    if not data or len(data) < PATH_HEADER_BYTES:
        return {"points": [], "position": None, "theta": None}

    version = data[0]
    path_id = (data[1] << 8) | data[2]
    force_update = data[3]
    ptype = data[4]
    total_count = int.from_bytes(data[5:9], "big")
    theta_raw = (data[9] << 8) | data[10]
    compressed_len = (data[11] << 8) | data[12]

    body = data[PATH_HEADER_BYTES:]
    if compressed_len:
        try:
            src = body[:compressed_len] if compressed_len <= len(body) else body
            body = uncompress_bounded(src, total_count * 4)
        except Exception as e:
            _LOGGER.debug("Path decompression failed: %s", e)
            return {"points": [], "position": None, "theta": None}

    points = []
    for i in range(0, len(body) - 3, 4):
        rx = (body[i] << 8) | body[i + 1]
        ry = (body[i + 2] << 8) | body[i + 3]
        # le bit de poids faible porte le drapeau, pas la coordonnée
        visible = (rx & 1) == 0 and (ry & 1) == 0
        x = round(_signed16(rx) / 10, 1)
        y = -round(_signed16(ry) / 10, 1)
        points.append({"x": x, "y": y, "visible": visible})

    position = None
    if points:
        position = [points[-1]["x"], points[-1]["y"]]

    return {
        "version": version,
        "path_id": path_id,
        "type": ptype,
        "force_update": force_update,
        "total_count": total_count,
        # theta est en dixièmes de degré dans le protocole
        "theta": round(theta_raw / 10, 1) if theta_raw else None,
        "points": points,
        "position": position,
    }


def areas_from_archive(data: bytes) -> list[dict]:
    """Zones sauvegardées lues dans « area_info.json » de l'archive de carte.

    C'est ce fichier qui porte les zones interdites, murs virtuels et zones de
    nettoyage mémorisés par le robot avec la carte.
    """
    import json

    raw = unwrap_container(data)
    if not is_tar(raw):
        return []
    members = extract_tar(raw)
    blob = None
    for name, content in members.items():
        if name.endswith("area_info.json"):
            blob = content
            break
    if not blob:
        return []
    try:
        info = json.loads(blob.decode("utf-8", "replace"))
    except Exception as e:
        _LOGGER.debug("area_info.json illisible : %s", e)
        return []

    # Le format n'est pas documenté : on accepte les structures usuelles et on
    # ne retient que ce qui ressemble à une liste de points.
    out: list[dict] = []

    def collect(node, kind):
        if isinstance(node, dict):
            pts = node.get("points") or node.get("vertexs") or node.get("vertices")
            if isinstance(pts, list) and len(pts) >= 2:
                coords = []
                for pt in pts:
                    if isinstance(pt, dict) and "x" in pt and "y" in pt:
                        coords.append([pt["x"], pt["y"]])
                    elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                        coords.append([pt[0], pt[1]])
                if len(coords) >= 2:
                    out.append({"type": kind, "points": coords, "from_map": True})
            for key, value in node.items():
                collect(value, node.get("type") or node.get("mode") or key)
        elif isinstance(node, list):
            for item in node:
                collect(item, kind)

    collect(info, "AREA")
    _LOGGER.debug("%d zone(s) lue(s) dans area_info.json", len(out))
    return out


class GridMapLayout(BaseLayout):
    """Carte enregistrée au format LDS (map.gridmap + map.segmentmap).

    Ce format ne réutilise PAS l'en-tête binaire de la carte temps réel. C'est
    une grille d'occupation brute : un octet par cellule dans map.gridmap
    (0 = inconnu, valeurs faibles = libre, valeurs fortes = obstacle) et un
    octet de numéro de pièce par cellule dans map.segmentmap. Les dimensions
    et l'origine viennent de navmap_bound.json.

    Le format exact n'étant pas documenté (le SDK le rend via un moteur natif),
    ce décodeur applique des heuristiques prudentes et reste tolérant : il vaut
    mieux une carte approximative qu'un plantage.
    """

    version = 100  # marqueur : ce n'est pas une version binaire standard

    def __init__(self, members: dict, bound: dict):
        self.raw = b""
        self.rooms = []
        self._grid = members.get("map.gridmap", b"")
        self._segments = members.get("map.segmentmap", b"")

        self.width = int(bound.get("width") or bound.get("w") or 0)
        self.height = int(bound.get("height") or bound.get("h") or 0)
        self.origin_x = float(bound.get("min_x", bound.get("origin_x", 0)) or 0)
        self.origin_y = float(bound.get("min_y", bound.get("origin_y", 0)) or 0)
        self.map_resolution = float(bound.get("resolution", 0.05) or 0.05)
        self.pile_x = float(bound.get("charger_x", 0) or 0)
        self.pile_y = float(bound.get("charger_y", 0) or 0)

        # Si les bornes ne donnent pas les dimensions, on les déduit d'un carré.
        if (not self.width or not self.height) and self._grid:
            import math

            side = int(math.isqrt(len(self._grid)))
            self.width = self.height = side
            _LOGGER.debug("Dimensions déduites du gridmap : %dx%d", side, side)

        if not self.width or not self.height:
            raise ValueError("gridmap sans dimensions exploitables")

    def to_image(self):
        import numpy as np
        from PIL import Image

        w, h = self.width, self.height
        grid = self._grid
        seg = self._segments
        area = w * h
        room_ids = None  # les pièces du segmentmap sont prises telles quelles

        rows = []
        for y in range(h):
            base = y * w
            row = []
            for x in range(w):
                i = base + x
                g = grid[i] if i < len(grid) else 0
                room = seg[i] if i < len(seg) else 0
                if g == 0:
                    row.append(BG_COLOR)              # inconnu
                elif g >= 200:
                    row.append(OBSTACLE_COLOR)        # obstacle
                elif room:
                    row.append(_room_color(room))     # pièce identifiée
                else:
                    row.append(FLOOR_COLOR)           # libre, sans pièce
            rows.append(row)

        image = Image.fromarray(np.array(rows, dtype=np.uint8))
        return self._upscale(image)


def _decode_grid_archive(members: dict):
    """Construit une carte depuis les fichiers gridmap/segmentmap d'une archive."""
    import json

    if "map.gridmap" not in members:
        return None
    bound = {}
    raw_bound = members.get("navmap_bound.json") or members.get("map_record.json")
    if raw_bound:
        try:
            bound = json.loads(raw_bound.decode("utf-8", "replace"))
            # navmap_bound.json est parfois {"data": {...}}
            if "data" in bound and isinstance(bound["data"], dict):
                bound = bound["data"]
        except Exception as e:
            _LOGGER.debug("navmap_bound.json illisible : %s", e)
    # position du chargeur depuis charger_pose.json si présente
    if "charger_pose.json" in members:
        try:
            cp = json.loads(members["charger_pose.json"].decode("utf-8", "replace"))
            bound.setdefault("charger_x", cp.get("x", 0))
            bound.setdefault("charger_y", cp.get("y", 0))
        except Exception:
            pass
    _LOGGER.info(
        "gridmap: bound=%s gridmap=%do segmentmap=%do",
        {k: bound.get(k) for k in ("width", "height", "resolution", "min_x", "min_y")},
        len(members.get("map.gridmap", b"")),
        len(members.get("map.segmentmap", b"")),
    )
    try:
        layout = GridMapLayout(members, bound)
        # cohérence : la taille du gridmap doit correspondre à width*height
        expected = layout.width * layout.height
        got = len(members.get("map.gridmap", b""))
        if got and expected and abs(got - expected) > expected * 0.1:
            _LOGGER.warning(
                "gridmap incohérent : %d octets pour %dx%d=%d attendus — "
                "les dimensions de navmap_bound.json sont probablement fausses",
                got, layout.width, layout.height, expected,
            )
        return layout
    except Exception as e:
        _LOGGER.warning("Décodage gridmap impossible : %s", e)
        return None


def room_vertices(room) -> list[tuple[int, int]]:
    """Sommets (en pixels carte) d'une pièce, depuis son vertex_str hexa.

    Chaque sommet occupe 4 octets : x sur 2, y sur 2, big-endian.
    """
    raw = getattr(room, "vertex_str", "") or ""
    out: list[tuple[int, int]] = []
    try:
        for i in range(0, len(raw) - 7, 8):
            out.append((int(raw[i : i + 4], 16), int(raw[i + 4 : i + 8], 16)))
    except ValueError:
        return []
    return out


def polygon_centroid(points):
    """Centre approximatif d'un polygone, pour y poser son nom."""
    if not points:
        return None
    n = len(points)
    return [sum(p[0] for p in points) / n, sum(p[1] for p in points) / n]


# ---------------------------------------------------------------------------
#  Dispatch
# ---------------------------------------------------------------------------
_DECODERS = {0: LayoutV0, 1: LayoutV1, 2: LayoutV2}


# ---------------------------------------------------------------------------
#  Conteneurs
# ---------------------------------------------------------------------------
#  Les cartes ENREGISTRÉES ne sont pas servies comme la carte temps réel :
#  l'endpoint de téléchargement renvoie le fichier compressé (gzip), et parfois
#  directement une image. Sans déballage, le premier octet lu comme numéro de
#  version donne 31 (0x1F, signature gzip) d'où "Map version 31 is not
#  supported".
IMAGE_MAGICS = {
    b"\x89PNG": "png",
    b"\xff\xd8\xff": "jpeg",
    b"GIF8": "gif",
    b"BM": "bmp",
}


def image_format(data: bytes) -> str | None:
    """Renvoie le format si les octets sont déjà une image, sinon None."""
    for magic, name in IMAGE_MAGICS.items():
        if data.startswith(magic):
            return name
    return None


def is_tar(data: bytes) -> bool:
    """Archive tar ? La signature « ustar » est à l'offset 257."""
    return len(data) > 262 and data[257:262] in (b"ustar", b"usta\x00")


def extract_tar(data: bytes) -> dict[str, bytes]:
    """Contenu d'une archive tar, {nom: octets}.

    Les cartes ENREGISTRÉES ne sont pas un binaire nu : le téléchargement
    renvoie une archive (souvent tar.gz) regroupant plusieurs fichiers, dont
    « area_info.json » (zones interdites, murs, zones de nettoyage) et le
    binaire de carte lui-même.
    """
    import io
    import tarfile

    out: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                name = member.name.lstrip("./")
                out[name] = handle.read()
    except Exception as e:
        _LOGGER.debug("Archive tar illisible : %s", e)
    return out


def unwrap_container(data: bytes) -> bytes:
    """Déballe gzip / zlib si nécessaire. Renvoie les octets tels quels sinon."""
    if not data or len(data) < 2:
        return data

    # gzip : 1f 8b
    if data[0] == 0x1F and data[1] == 0x8B:
        import gzip

        try:
            out = gzip.decompress(data)
            _LOGGER.debug("Carte gzip déballée : %d -> %d octets", len(data), len(out))
            return out
        except Exception as e:
            _LOGGER.warning("Déballage gzip impossible : %s", e)
            return data

    # zlib : 78 01 / 78 9c / 78 da
    if data[0] == 0x78 and data[1] in (0x01, 0x5E, 0x9C, 0xDA):
        import zlib

        try:
            out = zlib.decompress(data)
            _LOGGER.debug("Carte zlib déballée : %d -> %d octets", len(data), len(out))
            return out
        except Exception as e:
            _LOGGER.debug("Déballage zlib impossible : %s", e)
            return data

    return data


def detect_version(data: bytes) -> int | None:
    """Map format version, read from the first header byte."""
    return data[0] if data else None


def _header_looks_valid(data: bytes, offset: int) -> bool:
    """Vérifie qu'un en-tête de carte plausible commence à cet offset."""
    if offset + HEADER_BYTES > len(data):
        return False
    head = data[offset:]
    if head[0] not in _DECODERS:          # version connue
        return False
    if head[3] > 1:                        # type : 0 = layout, 1 = path
        return False
    width = (head[4] << 8) | head[5]
    height = (head[6] << 8) | head[7]
    if not (1 <= width <= 4000 and 1 <= height <= 4000):
        return False
    before = int.from_bytes(head[18:22], "big")
    if not (0 < before <= width * height * 4 + 65536):
        return False
    after = (head[22] << 8) | head[23]
    if after == 0:                         # une carte compressée a une longueur
        return False
    resolution = (head[12] << 8) | head[13]
    if resolution == 0:                    # résolution nulle = pas une carte
        return False
    # le tout premier octet d'un tar est 0x2e ('.'), jamais une version valide
    return True


def find_header_offset(data: bytes, search: int = 64) -> int | None:
    """Cherche le début de l'en-tête, qui n'est pas toujours à l'offset 0.

    Les fichiers de carte enregistrée peuvent comporter un préambule (le
    téléchargement renvoie parfois un conteneur avec quelques octets devant).
    Plutôt que d'échouer sur un faux numéro de version, on repère le premier
    en-tête cohérent.
    """
    for offset in range(0, min(search, max(0, len(data) - HEADER_BYTES)) + 1):
        if _header_looks_valid(data, offset):
            return offset
    return None


def supported_versions() -> list[int]:
    """Versions this module can decode."""
    return sorted(_DECODERS)


def decode_layout(data: bytes, allow_fallback: bool = True):
    """Decode a layout, selecting the implementation from its version.

    @param allow_fallback: on failure, retry with tuya-vacuum's decoder for the
        versions it supports. On by default, so a bug here degrades to the
        library's behaviour instead of losing the map entirely.
    """
    data = unwrap_container(data)

    if is_tar(data):
        members = extract_tar(data)
        _LOGGER.info("Archive de carte : %s", ", ".join(members) or "vide")

        # Format LDS enregistré : présence d'un gridmap -> chemin dédié, sans
        # tenter d'interpréter un membre comme une carte binaire (ce qui
        # déclenchait "bytearray index out of range" sur segmentmap).
        if "map.gridmap" in members:
            grid_layout = _decode_grid_archive(members)
            if grid_layout is not None:
                return grid_layout

        chosen = None
        # On ne cherche un en-tête binaire QUE dans les membres qui portent un
        # nom de carte binaire, jamais dans les .json ni les *map bruts.
        BINARY_NAMES = ("map.bin", "lay.bin", "map", "layout", "realtime")
        for name, content in members.items():
            base = name.rsplit("/", 1)[-1].lower()
            if base not in BINARY_NAMES:
                continue
            if content and _header_looks_valid(content, 0):
                chosen = content
                break
            off = find_header_offset(content) if content else None
            if off is not None:
                chosen = content[off:]
                break
        if chosen is None:
            # Pas de binaire de carte temps réel : c'est une carte enregistrée
            # au format LDS (gridmap/segmentmap). On la décode autrement.
            grid_layout = _decode_grid_archive(members)
            if grid_layout is not None:
                return grid_layout
            raise NotImplementedError(
                "Archive de carte sans binaire exploitable. Fichiers présents : "
                + (", ".join(members) or "aucun")
            )
        data = chosen

    fmt = image_format(data)
    if fmt:
        raise ValueError(
            f"Ces octets sont déjà une image {fmt}, pas une carte binaire "
            "(à afficher directement)"
        )

    version = detect_version(data)
    decoder = _DECODERS.get(version)

    if decoder is None or not _header_looks_valid(data, 0):
        offset = find_header_offset(data)
        if offset is None:
            preview = data[:24].hex(" ")
            raise NotImplementedError(
                f"Aucun en-tête de carte reconnu (premier octet {version}, "
                f"versions connues {supported_versions()}). "
                f"24 premiers octets : {preview}"
            )
        if offset:
            _LOGGER.info(
                "En-tête de carte trouvé à l'offset %d (préambule ignoré : %s)",
                offset,
                data[:offset].hex(" "),
            )
            data = data[offset:]
            version = detect_version(data)
        decoder = _DECODERS.get(version)
        if decoder is None:
            raise NotImplementedError(
                f"Map version {version} is not supported "
                f"(known versions: {supported_versions()})"
            )

    try:
        layout = decoder(data)
        _LOGGER.debug("Decoded map: %r", layout)
        return layout
    except Exception as e:
        if not allow_fallback or version not in (0, 1):
            raise
        _LOGGER.warning(
            "Decoding version %s failed (%s), falling back to tuya-vacuum",
            version,
            e,
        )
        from tuya_vacuum.map.layout import Layout

        return Layout(data)
