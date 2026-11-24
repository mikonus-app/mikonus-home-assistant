"""Validate the native Living Island world before storage."""

import re
from base64 import b64decode
from binascii import Error as InvalidBase64
from itertools import combinations, pairwise
from math import ceil, hypot, isfinite
from struct import error as InvalidBinary
from struct import iter_unpack

from .models import ContractError

SUPPORTED_VERSIONS = (1, 2, 3, 4, 5)
WORLD_FIELDS = ("worldPublication", "livingIsland")
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
EPSILON = 1e-9
PLANT_KEYS = (
    "outdoor_tree",
    "outdoor_shrub",
    "outdoor_hedge",
    "outdoor_grass",
    "outdoor_flower",
    "outdoor_perennial",
)


def check(condition, path, reason):
    if not condition:
        code = (
            "invalid_living_island"
            if path.startswith("livingIsland")
            else "invalid_world_publication"
        )
        raise ContractError(code, reason, path=path)


def matches(value, pattern):
    return isinstance(value, str) and bool(re.fullmatch(pattern, value))


def finite(value):
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def numbers(value, count):
    return isinstance(value, list) and len(value) == count and all(map(finite, value))


def string(value):
    return isinstance(value, str) and bool(value.strip())


def finite_fields(value, fields, path):
    for field in fields:
        check(finite(value.get(field)), f"{path}.{field}", "Expected a finite number.")


def affine(value):
    return numbers(value, 16) and [value[i] for i in (3, 7, 11, 15)] == [0, 0, 0, 1]


def validate_roof(roof):
    path = "livingIsland.roof"

    def unit(value):
        return finite(value) and 0 <= value <= 1

    def color(value):
        return numbers(value, 4) and all(map(unit, value))

    def decode(value, format_code):
        if not isinstance(value, str) or not value or len(value) > 640000:
            return None
        try:
            return [
                number
                for (number,) in iter_unpack(
                    format_code, b64decode(value, validate=True)
                )
            ]
        except (InvalidBase64, InvalidBinary, ValueError):
            return None

    check(
        isinstance(roof, dict)
        and roof.get("style") in ("flat", "gable", "hip", "shed"),
        path,
        "Expected a resolved roof style.",
    )
    finite_fields(roof, ("pitchDegrees", "overhangMeters", "baseElevationMeters"), path)
    check(
        2 <= roof["pitchDegrees"] <= 55
        and 0 <= roof["overhangMeters"] <= 1.2 + 1e-6
        and color(roof.get("facadeColor")),
        path,
        "Invalid roof dimensions or facade color.",
    )
    for field, limit in (("meshes", 32), ("materials", 16), ("parts", 256)):
        check(
            isinstance(roof.get(field), list) and 0 < len(roof[field]) <= limit,
            path,
            "Invalid roof mesh, material or instance list.",
        )
    vertices = indices_count = 0
    for mesh in roof["meshes"]:
        check(isinstance(mesh, dict), path, "Invalid roof mesh.")
        positions = decode(mesh.get("positions"), "<f")
        indices = decode(mesh.get("indices"), "<H")
        normals = decode(mesh.get("normals"), "<f") if "normals" in mesh else None
        check(
            isinstance(positions, list)
            and len(positions) >= 9
            and len(positions) % 3 == 0
            and all(map(finite, positions)),
            path,
            "Invalid roof vertices.",
        )
        size = len(positions) // 3
        check(
            isinstance(indices, list)
            and len(indices) >= 3
            and len(indices) % 3 == 0
            and all(type(i) is int and 0 <= i < size for i in indices)
            and ("normals" not in mesh or numbers(normals, len(positions))),
            path,
            "Invalid roof triangle geometry.",
        )
        vertices += size
        indices_count += len(indices)
    check(
        vertices <= 40000 and indices_count <= 240000,
        path,
        "Roof exceeds the geometry budget.",
    )
    for material in roof["materials"]:
        check(
            isinstance(material, dict)
            and color(material.get("color"))
            and unit(material.get("roughness"))
            and unit(material.get("metallic")),
            path,
            "Invalid roof material.",
        )
    for part in roof["parts"]:
        check(
            isinstance(part, dict) and affine(part.get("transform")),
            path,
            "Invalid roof instance.",
        )
        for index, field in (("meshIndex", "meshes"), ("materialIndex", "materials")):
            check(
                type(part.get(index)) is int and 0 <= part[index] < len(roof[field]),
                path,
                "Invalid roof instance reference.",
            )


def validate_seasonal(settings, path, string_lights=False):
    check(isinstance(settings, dict), path, "Expected seasonal settings.")
    for key, value in settings.items():
        if key == "seasonal.mode":
            valid = value in ("on", "off", "sunset")
        elif key == "seasonal.palette":
            valid = value in ("warm", "multi")
        else:
            number = (
                float(value)
                if matches(value, r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
                else -1
            )
            valid = (key == "seasonal.brightness" and 0 <= number <= 1) or (
                string_lights and key == "seasonal.spacing" and 0.15 <= number <= 1.5
            )
        check(valid, f"{path}.{key}", "Unsupported seasonal value.")


def validate_objects(island):
    objects = island.get("objects")
    check(
        isinstance(objects, list),
        "livingIsland.objects",
        "V3 requires an object array.",
    )
    seen = {item["id"] for item in island["plants"]}
    for i, item in enumerate(objects):
        path = f"livingIsland.objects[{i}]"
        check(
            isinstance(item, dict)
            and matches(item.get("id"), rf"object:{UUID}")
            and item["id"] not in seen,
            path,
            "Expected a unique native object ID.",
        )
        seen.add(item["id"])
        check(
            all(
                string(item.get(key)) for key in ("modelKey", "variantKey", "masterKey")
            )
            and item["modelKey"] not in PLANT_KEYS,
            path,
            "Invalid object catalog keys.",
        )
        check(
            affine(item.get("transform")),
            f"{path}.transform",
            "Expected an affine matrix.",
        )
        if "parts" in item:
            parts = item["parts"]
            check(
                isinstance(parts, list)
                and parts
                and all(
                    isinstance(part, dict)
                    and string(part.get("masterKey"))
                    and affine(part.get("transform"))
                    for part in parts
                ),
                f"{path}.parts",
                "Expected module masters and affine matrices.",
            )
        if "materialOverrides" in item:
            overrides = item["materialOverrides"]
            check(
                isinstance(overrides, list)
                and all(
                    isinstance(row, dict)
                    and string(row.get("slot"))
                    and numbers(row.get("color"), 4)
                    and 0 <= row["color"][3] <= 1
                    and finite(row.get("roughness"))
                    and finite(row.get("metallic"))
                    for row in overrides
                ),
                f"{path}.materialOverrides",
                "Expected a list of complete material overrides.",
            )
        if "seasonal" in item:
            validate_seasonal(item["seasonal"], f"{path}.seasonal")


def validate_details(island, contours, material_index):
    lines, surfaces = island.get("lines"), island.get("surfaces")
    check(
        isinstance(lines, list) and isinstance(surfaces, list),
        "livingIsland",
        "V4 requires line and surface arrays.",
    )
    validate_ids(surfaces, "livingIsland.surfaces")
    for i, surface in enumerate(surfaces):
        path = f"livingIsland.surfaces[{i}]"
        check(
            surface.get("kind") in ("lawn", "soil", "water", "other")
            and surface.get("source") == "surface",
            path,
            "Invalid authored surface.",
        )
        surface_height(surface, path)
        material_index(surface.get("materialIndex"), f"{path}.materialIndex")
        regions = surface.get("regions")
        check(isinstance(regions, list) and regions, path, "Expected visible regions.")
        for region in regions:
            contours.polygon(region, path)
    seen = set()
    for i, line in enumerate(lines):
        path = f"livingIsland.lines[{i}]"
        check(
            isinstance(line, dict)
            and matches(line.get("id"), rf"{UUID}(?::border:\d+)?")
            and line["id"] not in seen,
            path,
            "Expected a unique native line ID.",
        )
        seen.add(line["id"])
        kind, points = line.get("kind"), line.get("points")
        check(
            kind in ("hedge", "fence", "wall", "edging", "stringLights")
            and type(line.get("closed")) is bool,
            path,
            "Invalid line kind or closure.",
        )
        check(
            isinstance(points, list)
            and 2 <= len(points) <= 4096
            and all(numbers(point, 3) for point in points),
            path,
            "Invalid XYZ centerline.",
        )
        finite_fields(line, ("widthMeters", "heightMeters"), path)
        check(
            line["widthMeters"] > 0 and line["heightMeters"] > 0,
            path,
            "Expected positive dimensions.",
        )
        if "color" in line:
            check(numbers(line["color"], 4), path, "Expected finite RGBA.")
        if "seasonal" in line:
            check(
                kind == "stringLights", path, "Seasonal settings require string lights."
            )
            validate_seasonal(line["seasonal"], f"{path}.seasonal", string_lights=True)
        placements = line.get("thujaPlacements", [])
        if "thujaPlacements" in line:
            check(
                kind == "hedge"
                and isinstance(placements, list)
                and placements
                and all(numbers(row, 8) and min(row[4:]) > 0 for row in placements),
                path,
                "Invalid Thuja placements.",
            )
        contours.remaining -= len(points) + len(placements)
        if kind != "stringLights" and not placements:
            pairs = pairwise(points + points[:1] if line["closed"] else points)
            spacing = {"fence": 1.9, "edging": 0.75}.get(kind, 1.2)
            for a, b in pairs:
                length = hypot(a[0] - b[0], a[2] - b[2])
                check(
                    isfinite(length) and length <= 16000,
                    path,
                    "Outdoor line span exceeds the geometry budget.",
                )
                contours.remaining -= 2 * ceil(length / spacing)
        check(
            contours.remaining >= 0,
            path,
            "Outdoor line exceeds the world geometry budget.",
        )


def cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def sign(value):
    return 0 if abs(value) <= EPSILON else (1 if value > 0 else -1)


def between(a, b, c):
    return min(a, c) - EPSILON <= b <= max(a, c) + EPSILON


def on_segment(a, b, c):
    return all(between(x, y, z) for x, y, z in zip(a, b, c, strict=True))


def intersects(a, b, c, d):
    abc, abd = sign(cross(a, b, c)), sign(cross(a, b, d))
    cda, cdb = sign(cross(c, d, a)), sign(cross(c, d, b))
    return (
        (abc * abd == -1 and cda * cdb == -1)
        or (abc == 0 and on_segment(a, c, b))
        or (abd == 0 and on_segment(a, d, b))
        or (cda == 0 and on_segment(c, a, d))
        or (cdb == 0 and on_segment(c, b, d))
    )


def edges(ring):
    return list(pairwise([*ring, ring[0]]))


def contains(ring, point):
    x, z = point
    return (
        sum(
            (az > z) != (bz > z) and x < ax + (bx - ax) * (z - az) / (bz - az)
            for (ax, az), (bx, bz) in edges(ring)
        )
        % 2
        == 1
    )


def rings_intersect(left, right):
    right_edges = edges(right)
    return any(intersects(a, b, c, d) for a, b in edges(left) for c, d in right_edges)


class Contours:
    def __init__(self):
        self.remaining = 16000

    def ring(self, ring, outer, path):
        check(
            isinstance(ring, list) and len(ring) >= 3,
            path,
            "Expected at least three points.",
        )
        self.remaining -= len(ring)
        check(self.remaining >= 0, path, "Contour exceeds the host complexity limit.")
        check(
            all(numbers(point, 2) for point in ring),
            path,
            "Expected finite [X, Z] points.",
        )
        check(
            len({tuple(point) for point in ring}) == len(ring),
            path,
            "Repeated contour point.",
        )
        area = -sum(cross(ring[0], a, b) for a, b in pairwise(ring[1:]))
        check(
            finite(area) and (area if outer else -area) >= 2e-6,
            path,
            "Wrong winding or degenerate contour.",
        )
        segments = edges(ring)
        # Reject only intersections between non-adjacent edges.
        for i, (a, b) in enumerate(segments):
            end = len(segments) - 1 if i == 0 else len(segments)
            for j in range(i + 2, end):
                c, d = segments[j]
                check(not intersects(a, b, c, d), path, "Self-intersecting contour.")

    @staticmethod
    def holes(outer, holes, path):
        for hole in holes:
            check(
                contains(outer, hole[0]) and not rings_intersect(outer, hole),
                path,
                "Hole outside or touching the outer contour.",
            )
        for left, right in combinations(holes, 2):
            check(
                not contains(left, right[0])
                and not contains(right, left[0])
                and not rings_intersect(left, right),
                path,
                "Overlapping or touching holes.",
            )

    def polygon(self, value, path):
        check(
            isinstance(value, dict) and isinstance(value.get("holes"), list),
            path,
            "Expected a polygon with outer and holes.",
        )
        self.ring(value.get("outer"), True, f"{path}.outer")
        for i, hole in enumerate(value["holes"]):
            self.ring(hole, False, f"{path}.holes[{i}]")
        self.holes(value["outer"], value["holes"], path)


def validate_ids(entries, path):
    ids = set()
    for entry in entries:
        check(
            isinstance(entry, dict) and matches(entry.get("id"), UUID),
            path,
            "Expected lowercase UUIDs.",
        )
        check(entry["id"] not in ids, path, "Duplicate ID.")
        ids.add(entry["id"])
    return ids


def validate_materials(materials):
    for i, material in enumerate(materials):
        path = f"livingIsland.materials[{i}]"
        check(
            isinstance(material, dict)
            and string(material.get("key"))
            and string(material.get("variant")),
            path,
            "Expected a key and variant.",
        )
        finite_fields(material, ("tileSizeMeters", "roughness"), path)
        check(
            material["tileSizeMeters"] > 0 and 0 <= material["roughness"] <= 1,
            path,
            "Invalid tile size or roughness.",
        )
        if "tint" in material:
            check(
                numbers(material["tint"], 4)
                and all(0 <= v <= 1 for v in material["tint"]),
                path,
                "Expected a normalized RGBA tint.",
            )
        for key in (
            "boardWidthMeters",
            "jointWidthMeters",
            "grainSizeMeters",
            "segmentSpacingMeters",
        ):
            if key in material:
                check(
                    finite(material[key]) and material[key] >= 0,
                    f"{path}.{key}",
                    "Expected a nonnegative dimension.",
                )


def surface_height(surface, path):
    finite_fields(
        surface, ("topMeters", "bottomMeters", "textureRotationDegrees"), path
    )
    check(
        surface["topMeters"] >= surface["bottomMeters"], path, "Inverted height range."
    )


def validate_garden(island, contours, material_index):
    for key in ("beds", "paving", "plants"):
        check(
            isinstance(island.get(key), list),
            f"livingIsland.{key}",
            "Expected an array.",
        )
    check(
        island.get("plantCatalogVersion") == "1.0.0",
        "livingIsland.plantCatalogVersion",
        "Unsupported plant catalog.",
    )
    bed_ids = validate_ids(island["beds"], "livingIsland.beds")
    for key, kinds in (("beds", ("bed",)), ("paving", ("path", "driveway", "gravel"))):
        ids = set()
        for i, surface in enumerate(island[key]):
            path = f"livingIsland.{key}[{i}]"
            check(
                isinstance(surface, dict) and matches(surface.get("id"), UUID),
                path,
                "Expected a lowercase UUID.",
            )
            kind, source = surface.get("kind"), surface.get("source")
            check(
                kind in kinds
                and (source == "surface" or (source == "linear" and kind == "path")),
                path,
                "Invalid surface kind or source.",
            )
            identity = source, surface["id"]
            check(identity not in ids, path, "Duplicate source and ID.")
            ids.add(identity)
            surface_height(surface, path)
            material_index(surface.get("materialIndex"), path)
            regions = surface.get("regions")
            check(
                isinstance(regions, list) and regions, path, "Expected visible regions."
            )
            for j, region in enumerate(regions):
                contours.polygon(region, f"{path}.regions[{j}]")
    plant_ids = set()
    for i, plant in enumerate(island["plants"]):
        path = f"livingIsland.plants[{i}]"
        check(
            isinstance(plant, dict) and string(plant.get("id")),
            path,
            "Expected a plant ID.",
        )
        plant_id = plant["id"]
        check(plant_id not in plant_ids, path, "Duplicate plant ID.")
        plant_ids.add(plant_id)
        bed = re.fullmatch(rf"bed:({UUID}):[0-9]+-[0-9]+", plant_id)
        check(
            (plant.get("bedId") == bed[1] and bed[1] in bed_ids)
            if bed
            else ("bedId" not in plant and matches(plant_id, rf"object:{UUID}")),
            path,
            "Invalid plant owner.",
        )
        check(
            plant.get("modelKey") in PLANT_KEYS and string(plant.get("variantKey")),
            path,
            "Expected a catalog model and variant.",
        )
        matrix = plant.get("transform")
        check(
            numbers(matrix, 16) and [matrix[j] for j in (3, 7, 11, 15)] == [0, 0, 0, 1],
            f"{path}.transform",
            "Expected a column-major affine matrix.",
        )
        if "shade" in plant:
            check(
                finite(plant["shade"]) and plant["shade"] >= 0, path, "Invalid shade."
            )


def validate_island(island, scene, project_id, world):
    check(isinstance(island, dict), "livingIsland", "Expected a complete object.")
    check(
        type(island.get("schemaVersion")) is int
        and island["schemaVersion"] == world["schemaVersion"],
        "livingIsland.schemaVersion",
        "Version differs from world manifest.",
    )
    check(
        island.get("sceneId") == scene["sceneId"]
        and island.get("projectId") == project_id,
        "livingIsland",
        "Project identity differs from the indoor scene.",
    )
    check(
        island.get("snapshotId") == world["snapshotId"],
        "livingIsland.snapshotId",
        "Snapshot differs from world manifest.",
    )

    house = island.get("house")
    check(
        isinstance(house, dict),
        "livingIsland.house",
        "Expected native house placement.",
    )
    for key in ("referenceFloorId", "groundFloorId"):
        check(
            matches(house.get(key), rf"floor:{UUID}"),
            f"livingIsland.house.{key}",
            "Invalid floor ID.",
        )
    for key in ("indoorCenterXZ", "parcelAnchorXZ"):
        check(
            numbers(house.get(key), 2),
            f"livingIsland.house.{key}",
            "Expected a finite anchor.",
        )
    finite_fields(
        house,
        (
            "parcelRotationDegrees",
            "groundElevationMeters",
            "groundFloorElevationMeters",
            "parcelMetersPerIndoorMeter",
        ),
        "livingIsland.house",
    )
    check(
        0.25 <= house["parcelMetersPerIndoorMeter"] <= 4,
        "livingIsland.house",
        "Invalid horizontal scale.",
    )
    for floor in scene["floors"]:
        if floor["id"] == house["groundFloorId"] and finite(floor.get("elevation")):
            check(
                abs(floor["elevation"] - house["groundFloorElevationMeters"]) <= 1e-6,
                "livingIsland.house.groundFloorElevationMeters",
                "Differs from exported ground floor.",
            )

    for key in ("boundaries", "terraces", "materials"):
        check(
            isinstance(island.get(key), list),
            f"livingIsland.{key}",
            "Expected an array.",
        )
    boundaries, terraces, materials = (
        island[key] for key in ("boundaries", "terraces", "materials")
    )
    if world["schemaVersion"] == 1:
        check(
            (boundaries or terraces)
            and not any(
                key in island
                for key in ("beds", "paving", "plants", "plantCatalogVersion")
            ),
            "livingIsland",
            "V1 requires a boundary or terrace and has no garden fields.",
        )
    validate_ids(boundaries, "livingIsland.boundaries")
    validate_ids(terraces, "livingIsland.terraces")
    validate_materials(materials)

    def material_index(value, path):
        check(
            type(value) is int and 0 <= value < len(materials),
            path,
            "Invalid material index.",
        )

    contours = Contours()
    for i, boundary in enumerate(boundaries):
        path, kind = f"livingIsland.boundaries[{i}]", boundary.get("kind")
        check(
            kind in ("mainProperty", "detachedArea", "courtyard", "sharedAccess")
            and (kind == "mainProperty") == (i == 0),
            path,
            "Expected main property first.",
        )
        contours.polygon(boundary.get("polygon"), f"{path}.polygon")
    path = "livingIsland.terrain"
    if boundaries:
        terrain = island.get("terrain")
        check(
            isinstance(terrain, dict)
            and terrain.get("boundaryId") == boundaries[0]["id"],
            path,
            "Expected terrain for the main boundary.",
        )
        finite_fields(terrain, ("topMeters",), path)
        material_index(terrain.get("materialIndex"), path)
        check(
            not {"regions", "cutouts"} <= terrain.keys(),
            path,
            "Regions and cutouts are exclusive.",
        )
        if "regions" in terrain:
            check(isinstance(terrain["regions"], list), path, "Expected regions array.")
            for region in terrain["regions"]:
                contours.polygon(region, f"{path}.regions")
        if "cutouts" in terrain:
            check(isinstance(terrain["cutouts"], list), path, "Expected cutouts array.")
            for ring in terrain["cutouts"]:
                contours.ring(ring, False, f"{path}.cutouts")
            main = boundaries[0]["polygon"]
            contours.holes(main["outer"], main["holes"] + terrain["cutouts"], path)
    else:
        check("terrain" not in island, path, "Terrain requires a main boundary.")
    for i, terrace in enumerate(terraces):
        path = f"livingIsland.terraces[{i}]"
        surface_height(terrace, path)
        contours.polygon(terrace.get("polygon"), path)
        material_index(terrace.get("materialIndex"), path)
    version = world["schemaVersion"]
    if version >= 2:
        validate_garden(island, contours, material_index)
    if version >= 3:
        validate_objects(island)
    else:
        check("objects" not in island, "livingIsland.objects", "Objects require V3.")
    if version >= 4:
        validate_details(island, contours, material_index)
    else:
        check(
            not any(key in island for key in ("lines", "surfaces")),
            "livingIsland",
            "Line and surface arrays require V4.",
        )
    if "roof" in island:
        check(version >= 5, "livingIsland.roof", "Roofs require V5.")
        validate_roof(island["roof"])


def validate_world(record):
    """Validate both parts and return only the declared outdoor fields."""
    if "worldPublication" not in record:
        check(
            "livingIsland" not in record,
            "worldPublication",
            "Outdoor data requires a world manifest.",
        )
        return {}
    scene, world = record["scene"], record["worldPublication"]
    project = re.fullmatch(rf"mikonus:({UUID})", scene["sceneId"])
    check(
        scene["schemaVersion"] == 2 and project,
        "worldPublication",
        "Expected a native Dashboard Scene v2.",
    )
    check(
        isinstance(world, dict)
        and type(world.get("schemaVersion")) is int
        and world["schemaVersion"] in SUPPORTED_VERSIONS,
        "worldPublication.schemaVersion",
        "Unsupported Living Island version.",
    )
    check(
        matches(world.get("snapshotId"), r"[0-9a-f]{64}"),
        "worldPublication.snapshotId",
        "Expected a lowercase SHA-256 snapshot ID.",
    )
    has_island = "livingIsland" in record
    check(
        type(world.get("hasLivingIsland")) is bool
        and world["hasLivingIsland"] == has_island,
        "worldPublication.hasLivingIsland",
        "Outdoor data presence differs from the manifest.",
    )
    if has_island:
        validate_island(record["livingIsland"], scene, project[1], world)
    return {key: record[key] for key in WORLD_FIELDS if key in record}
