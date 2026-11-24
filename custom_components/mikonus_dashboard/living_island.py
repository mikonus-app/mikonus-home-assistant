"""Validate the native Living Island V1/V2 wire contract before storage."""

import re
from itertools import combinations, pairwise
from math import isfinite

from .models import ContractError

SUPPORTED_VERSIONS = (1, 2)
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
    if world["schemaVersion"] == 2:
        validate_garden(island, contours, material_index)


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
