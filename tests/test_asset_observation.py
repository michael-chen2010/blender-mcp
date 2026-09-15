"""Pure observation aggregation and material-graph classification tests."""

from __future__ import annotations

import types

from blender_mcp.bundled import asset_prepare_worker as worker


class FakeObject:
    def __init__(self, name: str, *, obj_type: str = "MESH", data=None):
        self.name = name
        self.type = obj_type
        self.data = data
        self.dimensions = (1.0, 2.0, 3.0)
        self.modifiers = []
        self.animation_data = None


class FakeSocket:
    def __init__(self, name: str, default_value=None):
        self.name = name
        self.default_value = default_value
        self.links = []
        self.is_linked = False


class FakeNode:
    def __init__(self, node_type: str, *, name: str | None = None):
        self.type = node_type
        self.bl_idname = node_type
        self.name = name or node_type
        self.inputs = {}
        self.outputs = []
        self.image = None
        self.is_active_output = node_type == "OUTPUT_MATERIAL"


class FakeLink:
    def __init__(self, from_node: FakeNode, to_socket: FakeSocket):
        self.from_node = from_node
        self.to_socket = to_socket
        to_socket.links.append(self)
        to_socket.is_linked = True


def _principled_material(name: str = "PBR", *, connected: bool = True, textured: bool = False):
    output = FakeNode("OUTPUT_MATERIAL")
    surface = FakeSocket("Surface")
    output.inputs["Surface"] = surface

    principled = FakeNode("BSDF_PRINCIPLED")
    principled.inputs = {
        "Base Color": FakeSocket("Base Color", (0.2, 0.3, 0.4, 1.0)),
        "Metallic": FakeSocket("Metallic", 0.7),
        "Roughness": FakeSocket("Roughness", 0.35),
        "Normal": FakeSocket("Normal", (0.0, 0.0, 0.0)),
    }
    nodes = [output, principled]
    if connected:
        FakeLink(principled, surface)
    if textured:
        image = types.SimpleNamespace(name="basecolor.png", size=(1024, 512))
        tex = FakeNode("TEX_IMAGE")
        tex.image = image
        FakeLink(tex, principled.inputs["Base Color"])
        nodes.append(tex)

    return types.SimpleNamespace(
        name=name,
        use_nodes=True,
        node_tree=types.SimpleNamespace(nodes=nodes),
    )


def _non_pbr_material(name: str = "Diffuse"):
    output = FakeNode("OUTPUT_MATERIAL")
    surface = FakeSocket("Surface")
    output.inputs["Surface"] = surface
    diffuse = FakeNode("BSDF_DIFFUSE")
    FakeLink(diffuse, surface)
    return types.SimpleNamespace(
        name=name,
        use_nodes=True,
        node_tree=types.SimpleNamespace(nodes=[output, diffuse]),
    )


def _mixed_material(name: str = "Mixed"):
    output = FakeNode("OUTPUT_MATERIAL")
    surface = FakeSocket("Surface")
    output.inputs["Surface"] = surface
    mix = FakeNode("MIX_SHADER")
    shader_a = FakeSocket("Shader A")
    shader_b = FakeSocket("Shader B")
    mix.inputs = {1: shader_a, 2: shader_b}
    principled = FakeNode("BSDF_PRINCIPLED")
    principled.inputs = {
        "Base Color": FakeSocket("Base Color", (0.8, 0.8, 0.8, 1.0)),
        "Metallic": FakeSocket("Metallic", 0.0),
        "Roughness": FakeSocket("Roughness", 0.5),
        "Normal": FakeSocket("Normal", (0.0, 0.0, 0.0)),
    }
    diffuse = FakeNode("BSDF_DIFFUSE")
    FakeLink(mix, surface)
    FakeLink(principled, shader_a)
    FakeLink(diffuse, shader_b)
    return types.SimpleNamespace(
        name=name,
        use_nodes=True,
        node_tree=types.SimpleNamespace(nodes=[output, mix, principled, diffuse]),
    )


def test_object_totals_are_complete_beyond_detail_limit_and_deterministic():
    objects = [FakeObject(f"Object{i:02d}") for i in range(15)]

    def inspect_mesh(obj, _scale_length):
        index = int(obj.name[-2:])
        return {
            "vertices": 10,
            "edges": 20,
            "polygons": 5,
            "triangles": 12,
            "hasUv": index % 2 == 0,
            "boundsMeters": [[float(index), 0.0, 0.0], [float(index + 1), 2.0, 3.0]],
            "dimensionsMeters": {"x": 1.0, "y": 2.0, "z": 3.0},
        }

    structure, geometry = worker.summarize_objects(
        objects,
        scale_length=1.0,
        detail_limit=10,
        mesh_inspector=inspect_mesh,
    )
    reversed_structure, reversed_geometry = worker.summarize_objects(
        list(reversed(objects)),
        scale_length=1.0,
        detail_limit=10,
        mesh_inspector=inspect_mesh,
    )

    assert structure["objectCount"] == 15
    assert structure["objects"]["totalCount"] == 15
    assert structure["objects"]["truncated"] is True
    assert len(structure["objects"]["items"]) == 10
    assert geometry["meshCount"] == 15
    assert geometry["vertexCount"] == 150
    assert geometry["triangleCount"] == 180
    assert geometry["hasUv"] is True
    assert geometry["dimensionsMeters"] == {"x": 15.0, "y": 2.0, "z": 3.0}
    assert structure == reversed_structure
    assert geometry == reversed_geometry


def test_active_linked_principled_is_pbr_and_reports_texture_evidence():
    result = worker.classify_material(_principled_material(textured=True))

    assert result["workflow"] == "PBR"
    assert "BSDF_PRINCIPLED" in result["shaderTypes"]
    assert result["baseColor"] == [0.2, 0.3, 0.4, 1.0]
    assert result["metallic"] == 0.7
    assert result["roughness"] == 0.35
    assert result["textureUsages"] == [
        {
            "channel": "base_color",
            "imageName": "basecolor.png",
            "width": 1024,
            "height": 512,
        }
    ]


def test_disconnected_principled_does_not_fake_pbr():
    material = _principled_material(connected=False)
    output = material.node_tree.nodes[0]
    diffuse = FakeNode("BSDF_DIFFUSE")
    material.node_tree.nodes.append(diffuse)
    FakeLink(diffuse, output.inputs["Surface"])

    result = worker.classify_material(material)

    assert result["workflow"] == "NON_PBR"
    assert "BSDF_PRINCIPLED" not in result["shaderTypes"]
    assert "BSDF_DIFFUSE" in result["shaderTypes"]


def test_mixed_connected_shader_graph_is_mixed():
    result = worker.classify_material(_mixed_material())

    assert result["workflow"] == "MIXED"
    assert {"BSDF_PRINCIPLED", "BSDF_DIFFUSE", "MIX_SHADER"}.issubset(
        set(result["shaderTypes"])
    )


def test_missing_active_surface_evidence_is_unknown():
    output = FakeNode("OUTPUT_MATERIAL")
    output.inputs["Surface"] = FakeSocket("Surface")
    material = types.SimpleNamespace(
        name="Unknown",
        use_nodes=True,
        node_tree=types.SimpleNamespace(nodes=[output, FakeNode("BSDF_PRINCIPLED")]),
    )

    result = worker.classify_material(material)

    assert result["workflow"] == "UNKNOWN"


def test_aggregate_material_workflow_requires_all_used_material_evidence():
    assert worker.aggregate_material_workflow(["PBR", "PBR"]) == "PBR"
    assert worker.aggregate_material_workflow(["NON_PBR", "NON_PBR"]) == "NON_PBR"
    assert worker.aggregate_material_workflow(["PBR", "NON_PBR"]) == "MIXED"
    assert worker.aggregate_material_workflow(["PBR", "UNKNOWN"]) == "MIXED"
    assert worker.aggregate_material_workflow(["UNKNOWN"]) == "UNKNOWN"


def test_deformation_summary_uses_asset_objects_not_global_action_names():
    action = object()
    rig = FakeObject("Rig", obj_type="ARMATURE", data=types.SimpleNamespace(shape_keys=None))
    rig.animation_data = types.SimpleNamespace(action=action, nla_tracks=[])
    mesh_data = types.SimpleNamespace(
        shape_keys=types.SimpleNamespace(key_blocks=[object(), object(), object()])
    )
    mesh = FakeObject("Mesh", data=mesh_data)
    mesh.modifiers = [types.SimpleNamespace(type="ARMATURE", object=rig)]

    summary = worker.summarize_deformation([mesh, rig])

    assert summary == {
        "rigged": True,
        "animated": True,
        "armatureCount": 1,
        "actionCount": 1,
        "shapeKeyCount": 2,
    }
