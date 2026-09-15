"""Isolated Blender worker for deterministic asset observation and MAIN preview."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Iterable

SUPPORTED_PROFILES = ("PREVIEW", "METADATA", "PUBLISH")
WORKER_PROTOCOL_VERSION = 2
OBSERVATION_SCHEMA_VERSION = 1
ANALYZER_VERSION = "blender-mcp-observation-v1"
PREVIEW_PRESET_VERSION = "asset-preview-v1"
OBJECT_DETAIL_LIMIT = 50
MATERIAL_DETAIL_LIMIT = 30
PREVIEW_WIDTH = 512
PREVIEW_HEIGHT = 512

_NON_PBR_SHADER_TYPES = {
    "BSDF_DIFFUSE",
    "BSDF_GLASS",
    "BSDF_GLOSSY",
    "BSDF_HAIR",
    "BSDF_HAIR_PRINCIPLED",
    "BSDF_REFRACTION",
    "BSDF_TOON",
    "BSDF_TRANSPARENT",
    "EMISSION",
    "SUBSURFACE_SCATTERING",
}
_SHADER_GRAPH_TYPES = _NON_PBR_SHADER_TYPES | {
    "BSDF_PRINCIPLED",
    "MIX_SHADER",
    "ADD_SHADER",
}
_PBR_INPUT_CHANNELS = {
    "Base Color": "base_color",
    "Metallic": "metallic",
    "Roughness": "roughness",
    "Normal": "normal",
    "Alpha": "alpha",
    "Emission Color": "emission",
    "Emission": "emission",
}
_LOD_RE = re.compile(r"(?:^|[_ .-])LOD(\d+)(?:$|[_ .-])", re.IGNORECASE)


def _import_bpy():
    import bpy  # type: ignore

    return bpy


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _bounded(items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    bounded_limit = max(0, int(limit))
    return {
        "totalCount": len(items),
        "truncated": len(items) > bounded_limit,
        "items": items[:bounded_limit],
    }


def _as_sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    values = getattr(value, "values", None)
    if callable(values):
        try:
            return list(values())
        except Exception:
            pass
    try:
        return list(value)
    except TypeError:
        return []


def _get_input(node: Any, name: str) -> Any | None:
    inputs = getattr(node, "inputs", None)
    if inputs is None:
        return None
    getter = getattr(inputs, "get", None)
    if callable(getter):
        try:
            found = getter(name)
            if found is not None:
                return found
        except Exception:
            pass
    for socket in _as_sequence(inputs):
        if getattr(socket, "name", None) == name:
            return socket
    return None


def _iter_inputs(node: Any) -> list[Any]:
    return _as_sequence(getattr(node, "inputs", None))


def _socket_links(socket: Any) -> list[Any]:
    if socket is None:
        return []
    try:
        if hasattr(socket, "is_linked") and not socket.is_linked:
            return []
    except Exception:
        pass
    return _as_sequence(getattr(socket, "links", None))


def _node_type(node: Any) -> str:
    return str(getattr(node, "type", None) or getattr(node, "bl_idname", "UNKNOWN"))


def _connected_nodes_from_socket(socket: Any) -> list[Any]:
    """Return only nodes contributing to the linked socket, breadth-first."""
    pending = []
    for link in _socket_links(socket):
        from_node = getattr(link, "from_node", None)
        if from_node is not None:
            pending.append(from_node)

    ordered: list[Any] = []
    seen: set[int] = set()
    while pending:
        node = pending.pop(0)
        identity = id(node)
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append(node)
        for input_socket in _iter_inputs(node):
            for link in _socket_links(input_socket):
                from_node = getattr(link, "from_node", None)
                if from_node is not None:
                    pending.append(from_node)
    return ordered


def _active_material_output(material: Any) -> Any | None:
    node_tree = getattr(material, "node_tree", None)
    outputs = [
        node
        for node in _as_sequence(getattr(node_tree, "nodes", None))
        if _node_type(node) == "OUTPUT_MATERIAL"
    ]
    if not outputs:
        return None
    for output in outputs:
        if bool(getattr(output, "is_active_output", False)):
            return output
    return outputs[0]


def _socket_default(socket: Any, fallback: Any = None) -> Any:
    if socket is None:
        return fallback
    return getattr(socket, "default_value", fallback)


def _float_value(value: Any) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _color_value(value: Any) -> list[float] | None:
    try:
        values = [round(float(component), 6) for component in value]
    except (TypeError, ValueError):
        return None
    return values[:4] if values else None


def _texture_usages(principled_nodes: Iterable[Any]) -> list[dict[str, Any]]:
    usages: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for principled in principled_nodes:
        for input_name, channel in _PBR_INPUT_CHANNELS.items():
            socket = _get_input(principled, input_name)
            if socket is None:
                continue
            for node in _connected_nodes_from_socket(socket):
                if _node_type(node) != "TEX_IMAGE":
                    continue
                image = getattr(node, "image", None)
                if image is None:
                    continue
                image_name = str(getattr(image, "name", ""))
                identity = (channel, image_name)
                if identity in seen:
                    continue
                seen.add(identity)
                size = _as_sequence(getattr(image, "size", None))
                usage: dict[str, Any] = {
                    "channel": channel,
                    "imageName": image_name or None,
                }
                if len(size) >= 2:
                    try:
                        usage["width"] = int(size[0])
                        usage["height"] = int(size[1])
                    except (TypeError, ValueError):
                        pass
                usages.append(usage)
    usages.sort(key=lambda item: (item["channel"], item.get("imageName") or ""))
    return usages


def classify_material(material: Any) -> dict[str, Any]:
    """Classify the shader graph that actually feeds the active Material Output."""
    name = str(getattr(material, "name", "Material"))
    if not bool(getattr(material, "use_nodes", False)):
        return {
            "name": name,
            "usesNodes": False,
            "shaderTypes": [],
            "textureUsages": [],
            "workflow": "NON_PBR",
            "pbrChannels": [],
        }

    output = _active_material_output(material)
    surface = _get_input(output, "Surface") if output is not None else None
    connected = _connected_nodes_from_socket(surface)
    connected_types = [_node_type(node) for node in connected]
    shader_types = sorted({kind for kind in connected_types if kind in _SHADER_GRAPH_TYPES})
    principled = [node for node in connected if _node_type(node) == "BSDF_PRINCIPLED"]
    has_non_pbr = any(kind in _NON_PBR_SHADER_TYPES for kind in connected_types)

    if principled and has_non_pbr:
        workflow = "MIXED"
    elif principled:
        workflow = "PBR"
    elif has_non_pbr:
        workflow = "NON_PBR"
    else:
        workflow = "UNKNOWN"

    result: dict[str, Any] = {
        "name": name,
        "usesNodes": True,
        "shaderTypes": shader_types,
        "textureUsages": _texture_usages(principled),
        "workflow": workflow,
        "pbrChannels": [],
    }
    if principled:
        primary = principled[0]
        base_color = _color_value(_socket_default(_get_input(primary, "Base Color")))
        metallic = _float_value(_socket_default(_get_input(primary, "Metallic")))
        roughness = _float_value(_socket_default(_get_input(primary, "Roughness")))
        if base_color is not None:
            result["baseColor"] = base_color
            result["pbrChannels"].append("base_color")
        if metallic is not None:
            result["metallic"] = metallic
            result["pbrChannels"].append("metallic")
        if roughness is not None:
            result["roughness"] = roughness
            result["pbrChannels"].append("roughness")
        for usage in result["textureUsages"]:
            if usage["channel"] not in result["pbrChannels"]:
                result["pbrChannels"].append(usage["channel"])
        result["pbrChannels"].sort()
    return result


def aggregate_material_workflow(workflows: Iterable[str]) -> str:
    values = [str(value) for value in workflows]
    if not values:
        return "UNKNOWN"
    unique = set(values)
    if unique == {"PBR"}:
        return "PBR"
    if unique == {"NON_PBR"}:
        return "NON_PBR"
    if unique == {"UNKNOWN"}:
        return "UNKNOWN"
    if "MIXED" in unique or len(unique) > 1:
        return "MIXED"
    return "UNKNOWN"


def _object_materials(obj: Any) -> list[Any]:
    materials: list[Any] = []
    seen: set[int] = set()
    data = getattr(obj, "data", None)
    for material in _as_sequence(getattr(data, "materials", None)):
        if material is not None and id(material) not in seen:
            seen.add(id(material))
            materials.append(material)
    for slot in _as_sequence(getattr(obj, "material_slots", None)):
        material = getattr(slot, "material", None)
        if material is not None and id(material) not in seen:
            seen.add(id(material))
            materials.append(material)
    return materials


def _object_dimensions(obj: Any, scale_length: float) -> dict[str, float] | None:
    dimensions = getattr(obj, "dimensions", None)
    if dimensions is None:
        return None
    values = _as_sequence(dimensions)
    if len(values) < 3:
        return None
    try:
        return {
            "x": round(abs(float(values[0])) * scale_length, 6),
            "y": round(abs(float(values[1])) * scale_length, 6),
            "z": round(abs(float(values[2])) * scale_length, 6),
        }
    except (TypeError, ValueError):
        return None


def _default_mesh_inspector(obj: Any, scale_length: float) -> dict[str, Any]:
    mesh = getattr(obj, "data", None)
    vertices = len(getattr(mesh, "vertices", ()) or ()) if mesh is not None else 0
    edges = len(getattr(mesh, "edges", ()) or ()) if mesh is not None else 0
    polygons = len(getattr(mesh, "polygons", ()) or ()) if mesh is not None else 0
    triangles = 0
    for polygon in getattr(mesh, "polygons", ()) or ():
        loop_total = int(getattr(polygon, "loop_total", 0) or 0)
        triangles += max(0, loop_total - 2)
    dimensions = _object_dimensions(obj, scale_length) or {"x": 0.0, "y": 0.0, "z": 0.0}
    bounds = [[0.0, 0.0, 0.0], [dimensions["x"], dimensions["y"], dimensions["z"]]]
    return {
        "vertices": vertices,
        "edges": edges,
        "polygons": polygons,
        "triangles": triangles,
        "hasUv": bool(getattr(mesh, "uv_layers", None)),
        "boundsMeters": bounds,
        "dimensionsMeters": dimensions,
    }


def summarize_objects(
    objects: Iterable[Any],
    *,
    scale_length: float,
    detail_limit: int = OBJECT_DETAIL_LIMIT,
    mesh_inspector: Callable[[Any, float], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Aggregate every object while bounding only the returned detail list."""
    ordered = sorted(list(objects), key=lambda obj: str(getattr(obj, "name", "")))
    inspect_mesh = mesh_inspector or _default_mesh_inspector
    details: list[dict[str, Any]] = []
    mesh_count = vertex_count = edge_count = polygon_count = triangle_count = 0
    has_uv = False
    lod_levels: set[int] = set()
    bounds_min: list[float] | None = None
    bounds_max: list[float] | None = None

    for obj in ordered:
        name = str(getattr(obj, "name", ""))
        obj_type = str(getattr(obj, "type", "UNKNOWN"))
        detail: dict[str, Any] = {"name": name, "type": obj_type}
        dimensions = _object_dimensions(obj, scale_length)
        if dimensions is not None:
            detail["dimensionsMeters"] = dimensions
        modifiers = sorted(
            {
                str(getattr(modifier, "type", "UNKNOWN"))
                for modifier in _as_sequence(getattr(obj, "modifiers", None))
            }
        )
        if modifiers:
            detail["modifiers"] = modifiers
        material_names = sorted(
            {str(getattr(material, "name", "")) for material in _object_materials(obj)}
        )
        if material_names:
            detail["materialNames"] = material_names

        lod_match = _LOD_RE.search(name)
        if lod_match:
            lod_levels.add(int(lod_match.group(1)))

        if obj_type == "MESH":
            mesh_count += 1
            stats = inspect_mesh(obj, scale_length)
            vertices = int(stats.get("vertices", 0) or 0)
            edges = int(stats.get("edges", 0) or 0)
            polygons = int(stats.get("polygons", 0) or 0)
            triangles = int(stats.get("triangles", 0) or 0)
            vertex_count += vertices
            edge_count += edges
            polygon_count += polygons
            triangle_count += triangles
            has_uv = has_uv or bool(stats.get("hasUv", False))
            detail["meshStats"] = {"vertices": vertices, "triangles": triangles}
            if stats.get("dimensionsMeters") is not None:
                detail["dimensionsMeters"] = stats["dimensionsMeters"]
            bounds = stats.get("boundsMeters")
            if bounds and len(bounds) >= 2:
                low = [float(component) for component in bounds[0][:3]]
                high = [float(component) for component in bounds[1][:3]]
                if bounds_min is None:
                    bounds_min = low
                    bounds_max = high
                else:
                    bounds_min = [min(bounds_min[i], low[i]) for i in range(3)]
                    bounds_max = [max(bounds_max[i], high[i]) for i in range(3)]
        details.append(detail)

    if bounds_min is None or bounds_max is None:
        dimensions = {"x": 0.0, "y": 0.0, "z": 0.0}
        normalized_bounds = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    else:
        extents = [max(0.0, bounds_max[i] - bounds_min[i]) for i in range(3)]
        dimensions = {
            "x": round(extents[0], 6),
            "y": round(extents[1], 6),
            "z": round(extents[2], 6),
        }
        normalized_bounds = [
            [0.0, 0.0, 0.0],
            [dimensions["x"], dimensions["y"], dimensions["z"]],
        ]

    structure = {
        "objectCount": len(ordered),
        "objects": _bounded(details, detail_limit),
    }
    geometry = {
        "meshCount": mesh_count,
        "vertexCount": vertex_count,
        "edgeCount": edge_count,
        "polygonCount": polygon_count,
        "triangleCount": triangle_count,
        "dimensionsMeters": dimensions,
        "boundingBox": normalized_bounds,
        "hasUv": has_uv,
        "lodCount": len(lod_levels),
    }
    return structure, geometry


def summarize_materials(
    objects: Iterable[Any], *, detail_limit: int = MATERIAL_DETAIL_LIMIT
) -> tuple[dict[str, Any], list[str]]:
    materials: list[Any] = []
    seen: set[int] = set()
    for obj in objects:
        for material in _object_materials(obj):
            if id(material) not in seen:
                seen.add(id(material))
                materials.append(material)
    materials.sort(key=lambda value: str(getattr(value, "name", "")))

    classified = [classify_material(material) for material in materials]
    workflows = [item["workflow"] for item in classified]
    pbr_channels = sorted(
        {channel for item in classified for channel in item.get("pbrChannels", [])}
    )
    image_names = {
        usage.get("imageName")
        for item in classified
        for usage in item.get("textureUsages", [])
        if usage.get("imageName")
    }
    public_items: list[dict[str, Any]] = []
    for item in classified:
        public_item = dict(item)
        public_item.pop("workflow", None)
        public_item.pop("pbrChannels", None)
        public_items.append(public_item)

    return (
        {
            "materialCount": len(materials),
            "textureCount": len(image_names),
            "items": _bounded(public_items, detail_limit),
            "materialWorkflow": aggregate_material_workflow(workflows),
            "pbrChannels": pbr_channels,
        },
        [str(getattr(material, "name", "")) for material in materials],
    )


def summarize_deformation(objects: Iterable[Any]) -> dict[str, Any]:
    ordered = list(objects)
    armatures = [obj for obj in ordered if str(getattr(obj, "type", "")) == "ARMATURE"]
    rigged = bool(armatures)
    action_ids: set[int] = set()
    animated = False
    shape_key_count = 0

    for obj in ordered:
        for modifier in _as_sequence(getattr(obj, "modifiers", None)):
            if str(getattr(modifier, "type", "")) == "ARMATURE":
                rigged = True
        for owner in (obj, getattr(obj, "data", None)):
            animation_data = getattr(owner, "animation_data", None)
            if animation_data is None:
                continue
            action = getattr(animation_data, "action", None)
            if action is not None:
                action_ids.add(id(action))
                animated = True
            for track in _as_sequence(getattr(animation_data, "nla_tracks", None)):
                strips = _as_sequence(getattr(track, "strips", None))
                if strips or track is not None:
                    animated = True
                for strip in strips:
                    strip_action = getattr(strip, "action", None)
                    if strip_action is not None:
                        action_ids.add(id(strip_action))
        shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
        key_blocks = _as_sequence(getattr(shape_keys, "key_blocks", None))
        if key_blocks:
            shape_key_count += max(0, len(key_blocks) - 1)
            if len(key_blocks) > 1:
                animated = animated or bool(getattr(shape_keys, "animation_data", None))

    return {
        "rigged": rigged,
        "animated": animated,
        "armatureCount": len(armatures),
        "actionCount": len(action_ids),
        "shapeKeyCount": shape_key_count,
    }


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _real_mesh_inspector(bpy: Any) -> Callable[[Any, float], dict[str, Any]]:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    from mathutils import Vector  # type: ignore

    def inspect(obj: Any, scale_length: float) -> dict[str, Any]:
        evaluated = obj.evaluated_get(depsgraph)
        try:
            mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        except TypeError:
            mesh = evaluated.to_mesh()
        try:
            mesh.calc_loop_triangles()
            points = [evaluated.matrix_world @ Vector(vertex.co) for vertex in mesh.vertices]
            if points:
                low = [min(point[index] for point in points) * scale_length for index in range(3)]
                high = [max(point[index] for point in points) * scale_length for index in range(3)]
            else:
                low = [0.0, 0.0, 0.0]
                high = [0.0, 0.0, 0.0]
            dimensions = {
                "x": round(max(0.0, high[0] - low[0]), 6),
                "y": round(max(0.0, high[1] - low[1]), 6),
                "z": round(max(0.0, high[2] - low[2]), 6),
            }
            return {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
                "triangles": len(mesh.loop_triangles),
                "hasUv": len(mesh.uv_layers) > 0,
                "boundsMeters": [
                    [round(float(value), 6) for value in low],
                    [round(float(value), 6) for value in high],
                ],
                "dimensionsMeters": dimensions,
            }
        finally:
            evaluated.to_mesh_clear()

    return inspect


def extract_observation(
    bpy: Any,
    *,
    prepare_id: str,
    source_kind: str,
    source_path: str,
    profile: str,
    object_detail_limit: int = OBJECT_DETAIL_LIMIT,
    material_detail_limit: int = MATERIAL_DETAIL_LIMIT,
) -> dict[str, Any]:
    """Extract deterministic SOURCE_ASSET facts before any preview rig exists."""
    objects = sorted(list(bpy.data.objects), key=lambda obj: obj.name)
    scene = bpy.context.scene
    unit_settings = getattr(scene, "unit_settings", None)
    scale_length = float(getattr(unit_settings, "scale_length", 1.0) or 1.0)
    structure, geometry = summarize_objects(
        objects,
        scale_length=scale_length,
        detail_limit=object_detail_limit,
        mesh_inspector=_real_mesh_inspector(bpy),
    )
    structure["collectionCount"] = len(bpy.data.collections)
    materials, material_names = summarize_materials(
        objects, detail_limit=material_detail_limit
    )
    deformation = summarize_deformation(objects)
    source = Path(source_path)

    return {
        "schemaVersion": OBSERVATION_SCHEMA_VERSION,
        "analyzerVersion": ANALYZER_VERSION,
        "prepareId": prepare_id,
        "profile": profile,
        "observationScope": "SOURCE_ASSET",
        "source": {
            "kind": source_kind,
            "displayName": source.name,
            "fileName": source.name,
            "sourceSize": source.stat().st_size,
            "sourceSha256": _file_sha256(source),
            "blenderVersion": bpy.app.version_string,
        },
        "structure": structure,
        "geometry": geometry,
        "materials": materials,
        "deformation": deformation,
        "evidenceSummary": {
            "objectNames": [obj.name for obj in objects],
            "materialNames": material_names,
        },
    }


def _preview_world_bounds(bpy: Any) -> tuple[Any, float]:
    from mathutils import Vector  # type: ignore

    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for obj in bpy.data.objects:
        if obj.type in {"CAMERA", "LIGHT"}:
            continue
        evaluated = obj.evaluated_get(depsgraph)
        bound_box = getattr(evaluated, "bound_box", None)
        if bound_box is None:
            continue
        points.extend(evaluated.matrix_world @ Vector(corner) for corner in bound_box)
    if not points:
        return Vector((0.0, 0.0, 0.0)), 2.0
    low = Vector(tuple(min(point[index] for point in points) for index in range(3)))
    high = Vector(tuple(max(point[index] for point in points) for index in range(3)))
    center = (low + high) * 0.5
    max_dimension = max(float(high[index] - low[index]) for index in range(3))
    return center, max(max_dimension, 0.1)


def _aim_at(obj: Any, target: Any) -> None:
    direction = target - obj.location
    if direction.length == 0:
        return
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def render_main_preview(bpy: Any, observation: dict[str, Any], output_path: str) -> dict[str, Any]:
    """Render the standard deterministic three-quarter MAIN image in worker memory."""
    from mathutils import Vector  # type: ignore

    scene = bpy.context.scene
    center, max_dimension = _preview_world_bounds(bpy)

    # Source lights would make the evidence machine/scene dependent. This worker
    # is isolated and never saves its modified memory back to the source file.
    for obj in bpy.data.objects:
        if obj.type == "LIGHT":
            obj.hide_render = True

    preview_collection = bpy.data.collections.new("__BLENDERMCP_PREVIEW__")
    scene.collection.children.link(preview_collection)

    camera_data = bpy.data.cameras.new("__BLENDERMCP_PREVIEW_CAMERA_DATA__")
    camera = bpy.data.objects.new("__BLENDERMCP_PREVIEW_CAMERA__", camera_data)
    preview_collection.objects.link(camera)
    camera_data.lens = 52.0
    camera_data.clip_start = max(0.001, max_dimension / 1000.0)
    camera_data.clip_end = max(1000.0, max_dimension * 20.0)
    direction = Vector((1.35, -1.55, 1.05)).normalized()
    camera.location = center + direction * (max_dimension * 2.5 + 0.75)
    _aim_at(camera, center)
    scene.camera = camera

    light_specs = (
        ("KEY", Vector((1.8, -1.4, 2.2)), 900.0, 1.8),
        ("FILL", Vector((-1.5, -0.6, 1.2)), 500.0, 2.2),
        ("RIM", Vector((0.4, 1.8, 2.0)), 700.0, 1.6),
    )
    energy_scale = max(0.5, min(4.0, math.sqrt(max_dimension)))
    for label, offset, energy, size_scale in light_specs:
        light_data = bpy.data.lights.new(f"__BLENDERMCP_PREVIEW_{label}_DATA__", "AREA")
        light_data.energy = energy * energy_scale
        light_data.shape = "DISK"
        light_data.size = max(0.5, max_dimension * size_scale)
        light = bpy.data.objects.new(f"__BLENDERMCP_PREVIEW_{label}__", light_data)
        preview_collection.objects.link(light)
        light.location = center + offset.normalized() * (max_dimension * 2.2 + 0.5)
        _aim_at(light, center)

    world = bpy.data.worlds.new("__BLENDERMCP_PREVIEW_WORLD__")
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background") if world.node_tree else None
    if background is not None:
        color_input = background.inputs.get("Color")
        strength_input = background.inputs.get("Strength")
        if color_input is not None:
            color_input.default_value = (0.055, 0.055, 0.055, 1.0)
        if strength_input is not None:
            strength_input.default_value = 0.35
    scene.world = world

    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except Exception:
        try:
            scene.render.engine = "BLENDER_EEVEE"
        except Exception:
            pass
    scene.render.resolution_x = PREVIEW_WIDTH
    scene.render.resolution_y = PREVIEW_HEIGHT
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = False
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    scene.render.filepath = str(target)
    bpy.ops.render.render(write_still=True)
    if not target.is_file():
        raise RuntimeError("Preview render completed without producing MAIN PNG")

    return {
        "width": PREVIEW_WIDTH,
        "height": PREVIEW_HEIGHT,
        "view": "THREE_QUARTER",
    }


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    prepare_id = str(job.get("prepareId") or "")
    profile = str(job.get("profile") or "")
    source_path = str(job.get("sourcePath") or "")
    source_kind = str(job.get("sourceKind") or "BLEND_FILE")
    preview_path = str(job.get("previewPath") or "")
    result_path = str(job.get("resultPath") or "")

    if not prepare_id:
        raise ValueError("job.prepareId is required")
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(f"Unsupported profile: {profile}")
    if not source_path or not Path(source_path).is_file():
        raise FileNotFoundError(f"job.sourcePath does not exist: {source_path}")
    if not preview_path:
        raise ValueError("job.previewPath is required")
    if not result_path:
        raise ValueError("job.resultPath is required")

    started = time.perf_counter()
    bpy = _import_bpy()
    try:
        open_started = time.perf_counter()
        bpy.ops.wm.open_mainfile(filepath=source_path)
        open_ms = _elapsed_ms(open_started)

        inspect_started = time.perf_counter()
        observation = extract_observation(
            bpy,
            prepare_id=prepare_id,
            source_kind=source_kind,
            source_path=source_path,
            profile=profile,
        )
        inspect_ms = _elapsed_ms(inspect_started)

        preview_started = time.perf_counter()
        preview_evidence = render_main_preview(bpy, observation, preview_path)
        preview_ms = _elapsed_ms(preview_started)
        observation["previewEvidence"] = {
            "presetVersion": PREVIEW_PRESET_VERSION,
            "main": preview_evidence,
            "supplementalViews": [],
        }

        result = {
            "status": "READY",
            "prepareId": prepare_id,
            "profile": profile,
            "observation": observation,
            "previewPath": preview_path,
            "timings": {
                "openMs": open_ms,
                "inspectMs": inspect_ms,
                "previewMs": preview_ms,
                "totalMs": _elapsed_ms(started),
            },
        }
        _write_json(result_path, result)
        return result
    except Exception as exc:
        failed = {
            "status": "FAILED",
            "prepareId": prepare_id,
            "profile": profile,
            "error": {
                "code": "WORKER_FAILED",
                "message": str(exc),
                "type": type(exc).__name__,
            },
            "timings": {"totalMs": _elapsed_ms(started)},
        }
        try:
            _write_json(result_path, failed)
        except Exception:
            pass
        raise


def build_status() -> dict[str, object]:
    return {
        "workerProtocolVersion": WORKER_PROTOCOL_VERSION,
        "supportedProfiles": list(SUPPORTED_PROFILES),
        "observationSchemaVersion": OBSERVATION_SCHEMA_VERSION,
        "analyzerVersion": ANALYZER_VERSION,
        "previewPresetVersion": PREVIEW_PRESET_VERSION,
    }


def _script_args(argv: list[str] | None) -> list[str]:
    if argv is not None:
        return argv
    values = list(sys.argv[1:])
    if "--" in values:
        values = values[values.index("--") + 1 :]
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BlenderMCP asset preparation worker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--status",
        action="store_true",
        help="Print worker capability metadata as JSON.",
    )
    group.add_argument("--job", help="Path to a prepare job JSON file.")
    args = parser.parse_args(_script_args(argv))
    if args.status:
        print(json.dumps(build_status(), sort_keys=True))
        return 0

    job_path = Path(args.job)
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
        result = run_job(job)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "code": "WORKER_FAILED",
                    "message": str(exc),
                    "type": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": result["status"],
                "prepareId": result["prepareId"],
                "profile": result["profile"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
