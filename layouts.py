"""Independent API validation; never instantiate arbitrary engine data."""
import hashlib
import copy
import json
import math
import re

START = {"position": [0, 4, 0], "yaw_degrees": 180}
STOCK = {"block": 60, "wedge": 12, "cylinder": 6, "truss": 6, "hazard": 10,
         "checkpoint": 8, "finish": 1, "launcher": 1, "conveyor": 1}
TYPES = {"block", "wedge", "cylinder", "hazard", "checkpoint", "finish"}
MINIMUM, MAXIMUM = (-60, -2, -8), (60, 60, 112)


def normalize_empty_tables(layout):
    """Luau has one empty table type; accept only empty container ambiguities."""
    result = copy.deepcopy(layout)
    if isinstance(result, dict):
        for key in ("parts", "checkpoint_order"):
            if result.get(key) == {}:
                result[key] = []
        if isinstance(result.get("parts"), list):
            for part in result["parts"]:
                if isinstance(part, dict) and part.get("properties") == []:
                    part["properties"] = {}
    return result


def number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def triple(value, low, high):
    return isinstance(value, list) and len(value) == 3 and all(number(v, low, high) for v in value)


def canonical(value):
    def normalize(v):
        if isinstance(v, float) and v.is_integer():
            return int(v)
        if isinstance(v, list):
            return [normalize(item) for item in v]
        if isinstance(v, dict):
            return {key: normalize(item) for key, item in v.items()}
        return v
    result = normalize(value)
    if isinstance(result, dict) and "parts" in result:
        result["parts"] = sorted(result["parts"], key=lambda part: part["id"])
    return json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def content_hash(layout):
    return hashlib.sha256(canonical(layout).encode()).hexdigest()


def initial_layout():
    pieces = []
    for kind, position, colour in [("block", [0, 0, 12], [90, 170, 230]),
                                    ("checkpoint", [0, 0, 24], [75, 210, 170]),
                                    ("finish", [0, 0, 36], [245, 205, 90])]:
        pieces.append(dict(id="starter-" + kind, label=kind, type=kind, position=position,
                           rotation_degrees=[0, 0, 0], size=[8, 1, 8], colour_rgb=colour,
                           properties={"respawn_yaw": 180} if kind == "checkpoint" else {}))
    return dict(schema_version=1, rules_version=1, start=START.copy(), parts=pieces,
                checkpoint_order=["starter-checkpoint"])


def validate(layout, stock, limit=150):
    def reject(reason):
        raise ValueError(reason)
    if not isinstance(layout, dict) or set(layout) != {"schema_version", "rules_version", "start", "parts", "checkpoint_order"}:
        reject("Unsupported layout fields")
    if type(layout["schema_version"]) is not int or layout["schema_version"] != 1 or type(layout["rules_version"]) is not int or layout["rules_version"] != 1:
        reject("Unsupported schema or rules version")
    start = layout["start"]
    if not isinstance(start, dict) or set(start) != set(START) or not triple(start["position"], -10000, 10000) or not number(start["yaw_degrees"], -36000, 36000) or start != START:
        reject("The entrance is fixed")
    parts, order = layout["parts"], layout["checkpoint_order"]
    if not isinstance(parts, list) or len(parts) > limit or not isinstance(order, list):
        reject("Object limit or invalid part list")
    used, ids, checkpoints = {}, set(), set()
    fields = {"id", "label", "type", "position", "rotation_degrees", "size", "colour_rgb", "properties"}
    for part in parts:
        if not isinstance(part, dict) or set(part) != fields:
            reject("Unsupported part fields")
        pid, kind = part["id"], part["type"]
        if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", pid) or pid in ids:
            reject("Invalid or duplicate part ID")
        ids.add(pid)
        if not isinstance(kind, str) or kind not in TYPES:
            reject("Unsupported piece type")
        if not isinstance(part["label"], str) or len(part["label"].encode()) > 40 or any(ord(c) < 32 or ord(c) == 127 for c in part["label"]):
            reject("Invalid label")
        if not triple(part["position"], -10000, 10000) or not triple(part["rotation_degrees"], -36000, 36000) or not triple(part["size"], .5, 40) or not triple(part["colour_rgb"], 0, 255):
            reject("Invalid transform or colour")
        props = part["properties"]
        if not isinstance(props, dict):
            reject("Invalid piece properties")
        if kind == "checkpoint":
            if set(props) != {"respawn_yaw"} or not number(props["respawn_yaw"], -36000, 36000):
                reject("Invalid checkpoint direction")
            checkpoints.add(pid)
        elif props:
            reject("Unsupported piece properties")
        x, y, z = map(math.radians, part["rotation_degrees"])
        sx, cx, sy, cy, sz, cz = math.sin(x), math.cos(x), math.sin(y), math.cos(y), math.sin(z), math.cos(z)
        matrix = ((cy*cz+sy*sx*sz, -cy*sz+sy*sx*cz, sy*cx),
                  (cx*sz, cx*cz, -sx), (-sy*cz+cy*sx*sz, sy*sz+cy*sx*cz, cy*cx))
        extents = [sum(abs(row[j])*part["size"][j]/2 for j in range(3)) for row in matrix]
        p = part["position"]
        if any(p[i]-extents[i] < MINIMUM[i]-1e-5 or p[i]+extents[i] > MAXIMUM[i]+1e-5 for i in range(3)):
            reject("Outside plot bounds")
        if p[0]-extents[0] < 8 and p[0]+extents[0] > -8 and p[1]-extents[1] < 8 and p[1]+extents[1] > -2 and p[2]-extents[2] < 6 and p[2]+extents[2] > -6:
            reject("Reserved entrance area")
        used[kind] = used.get(kind, 0) + 1
        if used[kind] > stock.get(kind, 0) or kind == "finish" and used[kind] > 1:
            reject("No copies remaining: " + kind)
    if any(not isinstance(pid, str) for pid in order) or len(order) != len(set(order)) or set(order) != checkpoints:
        reject("Invalid checkpoint order")
    return layout
