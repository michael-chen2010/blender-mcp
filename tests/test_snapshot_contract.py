"""Contract coverage for zero-mutation current-selection Blender snapshots."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from conftest import ROOT_ADDON


class FakeID:
    def __init__(self, name: str):
        self.name = name

    __hash__ = object.__hash__


class FakeMaterial(FakeID):
    def __init__(self, name: str, image: FakeID | None = None):
        super().__init__(name)
        self.use_nodes = image is not None
        self.node_tree = (
            types.SimpleNamespace(nodes=[types.SimpleNamespace(image=image)])
            if image is not None
            else None
        )


class FakeVertex:
    def __init__(self, co):
        self.co = tuple(co)


class FakeMesh(FakeID):
    def __init__(self, name: str, materials=()):
        super().__init__(name)
        self.materials = list(materials)
        self.vertices = [FakeVertex((0.0, 0.0, 0.0)), FakeVertex((1.0, 0.0, 0.0))]
        self.edges = []
        self.loops = []
        self.polygons = []


class FakeObject(FakeID):
    def __init__(self, name: str, *, obj_type: str = "EMPTY", data=None):
        super().__init__(name)
        self.type = obj_type
        self.data = data
        self.children = []
        self.parent = None
        self.modifiers = []
        self.constraints = []
        self.users_collection = []
        self.location = (0.0, 0.0, 0.0)
        self.rotation_euler = (0.0, 0.0, 0.0)
        self.scale = (1.0, 1.0, 1.0)


def _load_addon(monkeypatch, *, selected_objects=(), all_objects=(), libraries=None):
    scene = types.SimpleNamespace(
        name="Scene",
        objects=list(all_objects),
        blendermcp_use_polyhaven=False,
        blendermcp_use_hyper3d=False,
        blendermcp_use_sketchfab=False,
        blendermcp_use_polypizza=False,
        blendermcp_use_hunyuan3d=False,
    )
    active = selected_objects[0] if selected_objects else None

    bpy = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace(
        scene=scene,
        selected_objects=list(selected_objects),
        view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=active)),
    )
    bpy.data = types.SimpleNamespace(
        filepath="C:/projects/source.blend",
        is_dirty=True,
        objects=list(all_objects),
        libraries=libraries or types.SimpleNamespace(write=lambda *_a, **_k: None),
    )
    bpy.ops = types.SimpleNamespace(
        wm=types.SimpleNamespace(
            save_as_mainfile=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("snapshot must never call save_as_mainfile")
            )
        )
    )
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )

    props = types.ModuleType("bpy.props")
    for name in (
        "BoolProperty",
        "EnumProperty",
        "FloatProperty",
        "IntProperty",
        "StringProperty",
    ):
        setattr(props, name, lambda **_kwargs: None)
    bpy.props = props

    handlers = types.ModuleType("bpy.app.handlers")
    handlers.persistent = lambda fn: fn
    handlers.undo_post = []
    handlers.redo_post = []
    handlers.depsgraph_update_post = []

    app = types.ModuleType("bpy.app")
    app.version = (4, 5, 0)
    app.version_string = "4.5.0"
    app.binary_path = "C:/Program Files/Blender Foundation/Blender/blender.exe"
    app.background = False
    app.handlers = handlers
    app.timers = types.SimpleNamespace(
        is_registered=lambda *_a, **_k: False,
        register=lambda *_a, **_k: None,
        unregister=lambda *_a, **_k: None,
    )
    bpy.app = app

    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", props)
    monkeypatch.setitem(sys.modules, "bpy.app", app)
    monkeypatch.setitem(sys.modules, "bpy.app.handlers", handlers)
    monkeypatch.setitem(sys.modules, "mathutils", types.ModuleType("mathutils"))

    requests = types.ModuleType("requests")
    requests.utils = types.SimpleNamespace(default_headers=dict)
    requests.exceptions = types.SimpleNamespace(Timeout=TimeoutError)
    monkeypatch.setitem(sys.modules, "requests", requests)

    spec = importlib.util.spec_from_file_location("blender_mcp_snapshot_test", ROOT_ADDON)
    addon = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(addon)
    return addon, bpy


def _asset_fixture():
    image = FakeID("BaseColor.png")
    material = FakeMaterial("Paint", image)
    mesh = FakeMesh("RootMesh", [material])
    armature_data = FakeID("RigData")

    root = FakeObject("Root", obj_type="MESH", data=mesh)
    child = FakeObject("Child", obj_type="MESH", data=FakeMesh("ChildMesh"))
    loose = FakeObject("Loose", obj_type="MESH", data=FakeMesh("LooseMesh"))
    armature = FakeObject("Rig", obj_type="ARMATURE", data=armature_data)
    target = FakeObject("ConstraintTarget")
    camera = FakeObject("UnrelatedCamera", obj_type="CAMERA", data=FakeID("CameraData"))
    light = FakeObject("UnrelatedLight", obj_type="LIGHT", data=FakeID("LightData"))

    root.children.append(child)
    child.parent = root
    child.modifiers.append(types.SimpleNamespace(type="ARMATURE", object=armature))
    loose.constraints.append(types.SimpleNamespace(type="COPY_LOCATION", target=target))

    return {
        "selected": [root, loose],
        "all": [root, child, loose, armature, target, camera, light],
        "root": root,
        "child": child,
        "loose": loose,
        "armature": armature,
        "target": target,
        "mesh": mesh,
        "material": material,
        "image": image,
        "armature_data": armature_data,
        "camera": camera,
        "light": light,
    }


def test_snapshot_capability_is_protocol_v7_and_advertised(monkeypatch):
    addon, _bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    info = server.get_addon_info()

    assert addon.ADDON_PROTOCOL_VERSION == 7
    assert "create_blend_snapshot" in info["capabilities"]
    assert "create_blend_snapshot_explicit_roots" in info["capabilities"]
    assert "discover_scene_asset_roots" in info["capabilities"]


def test_asset_closure_includes_hierarchy_and_required_dependencies(monkeypatch):
    fixture = _asset_fixture()
    addon, _bpy = _load_addon(
        monkeypatch,
        selected_objects=fixture["selected"],
        all_objects=fixture["all"],
    )
    server = addon.BlenderMCPServer()

    datablocks = set(
        server._collect_snapshot_datablocks(
            fixture["selected"],
            closure_mode="ASSET_CLOSURE",
        )
    )

    for key in (
        "root",
        "child",
        "loose",
        "armature",
        "target",
        "mesh",
        "material",
        "image",
        "armature_data",
    ):
        assert fixture[key] in datablocks, f"missing required snapshot dependency: {key}"
    assert fixture["camera"] not in datablocks
    assert fixture["light"] not in datablocks


def test_include_descendants_does_not_pull_modifier_or_constraint_targets(monkeypatch):
    fixture = _asset_fixture()
    addon, _bpy = _load_addon(
        monkeypatch,
        selected_objects=fixture["selected"],
        all_objects=fixture["all"],
    )
    server = addon.BlenderMCPServer()

    datablocks = set(
        server._collect_snapshot_datablocks(
            fixture["selected"],
            closure_mode="INCLUDE_DESCENDANTS",
        )
    )

    assert fixture["child"] in datablocks
    assert fixture["armature"] not in datablocks
    assert fixture["target"] not in datablocks


def test_create_blend_snapshot_writes_asset_closure_without_save_as_mainfile(
    monkeypatch, tmp_path: Path
):
    fixture = _asset_fixture()
    recorded = {}

    def write(filepath, datablocks, **kwargs):
        recorded["filepath"] = filepath
        recorded["datablocks"] = set(datablocks)
        recorded["kwargs"] = kwargs
        Path(filepath).write_bytes(b"BLENDER-snapshot")

    addon, bpy = _load_addon(
        monkeypatch,
        selected_objects=fixture["selected"],
        all_objects=fixture["all"],
        libraries=types.SimpleNamespace(write=write),
    )
    server = addon.BlenderMCPServer()
    target = tmp_path / "asset-snapshot.blend"
    before = (bpy.data.filepath, bpy.data.is_dirty, tuple(bpy.context.selected_objects))

    result = server.create_blend_snapshot(
        filepath=str(target),
        selectionMode="CURRENT_SELECTION",
        closureMode="ASSET_CLOSURE",
    )

    after = (bpy.data.filepath, bpy.data.is_dirty, tuple(bpy.context.selected_objects))
    assert after == before
    assert recorded["filepath"] == str(target.resolve())
    assert fixture["child"] in recorded["datablocks"]
    assert fixture["camera"] not in recorded["datablocks"]
    assert set(result["rootObjectNames"]) == {"Root", "Loose"}
    assert set(result["includedObjectNames"]) == {
        "Root",
        "Child",
        "Loose",
        "Rig",
        "ConstraintTarget",
    }
    assert result["sourceBlendPath"] == "C:/projects/source.blend"
    assert result["blenderVersion"] == "4.5.0"
    assert len(result["sourceFingerprint"]) == 64


def test_snapshot_command_is_registered_on_main_thread_dispatch(monkeypatch, tmp_path: Path):
    fixture = _asset_fixture()

    def write(filepath, _datablocks, **_kwargs):
        Path(filepath).write_bytes(b"BLENDER-snapshot")

    addon, _bpy = _load_addon(
        monkeypatch,
        selected_objects=fixture["selected"],
        all_objects=fixture["all"],
        libraries=types.SimpleNamespace(write=write),
    )
    server = addon.BlenderMCPServer()

    response = server._execute_command_internal(
        {
            "type": "create_blend_snapshot",
            "params": {
                "filepath": str(tmp_path / "dispatch.blend"),
                "selectionMode": "CURRENT_SELECTION",
                "closureMode": "ASSET_CLOSURE",
            },
        }
    )

    assert response["status"] == "success"
    assert response["result"]["includedObjectNames"]


def test_server_snapshot_adapter_sends_exact_addon_command(monkeypatch, tmp_path: Path):
    from blender_mcp import server as mcp_server

    connection = types.SimpleNamespace()
    calls = []

    def send_command(command_type, params):
        calls.append((command_type, params))
        return {"filepath": params["filepath"], "includedObjectNames": ["Root"]}

    connection.send_command = send_command
    monkeypatch.setattr(mcp_server, "get_blender_connection", lambda: connection)
    target = tmp_path / "adapter.blend"

    result = mcp_server.create_blend_snapshot(
        str(target), closure_mode="ASSET_CLOSURE"
    )

    assert result["filepath"] == str(target)
    assert calls == [
        (
            "create_blend_snapshot",
            {
                "filepath": str(target),
                "selectionMode": "CURRENT_SELECTION",
                "closureMode": "ASSET_CLOSURE",
            },
        )
    ]


def test_discover_scene_asset_roots_returns_hierarchy_roots_and_scene_fingerprint(monkeypatch):
    fixture = _asset_fixture()
    addon, bpy = _load_addon(monkeypatch, all_objects=fixture["all"])
    server = addon.BlenderMCPServer()

    result = server.discover_scene_asset_roots()

    assert len(result["sceneFingerprint"]) == 64
    assert result["sceneObjectCount"] == len(fixture["all"])
    roots = {item["objectName"]: item for item in result["roots"]}
    assert set(roots) == {"Root", "Loose"}
    assert roots["Root"]["descendantObjectNames"] == ["Child"]
    assert roots["Root"]["geometryObjectCount"] == 2
    assert roots["Loose"]["geometryObjectCount"] == 1
    assert len({item["rootObjectId"] for item in roots.values()}) == 2
    before_ids = {name: item["rootObjectId"] for name, item in roots.items()}
    bpy.context.selected_objects[:] = [fixture["loose"]]
    selection_independent = server.discover_scene_asset_roots()
    assert selection_independent["sceneFingerprint"] == result["sceneFingerprint"]
    assert {item["objectName"]: item["rootObjectId"] for item in selection_independent["roots"]} == before_ids


def test_scene_fingerprint_changes_when_mesh_geometry_changes(monkeypatch):
    fixture = _asset_fixture()
    addon, _bpy = _load_addon(monkeypatch, all_objects=fixture["all"])
    server = addon.BlenderMCPServer()
    before = server.discover_scene_asset_roots()

    fixture["mesh"].vertices[0].co = (0.25, 0.0, 0.0)
    after = server.discover_scene_asset_roots()

    assert after["sceneFingerprint"] != before["sceneFingerprint"]


def test_scene_fingerprint_changes_when_modifier_settings_change(monkeypatch):
    fixture = _asset_fixture()
    modifier = types.SimpleNamespace(
        name="Subdivision",
        type="SUBSURF",
        levels=2,
        render_levels=3,
        show_viewport=True,
        show_render=True,
        bl_rna=types.SimpleNamespace(
            properties=[
                types.SimpleNamespace(identifier="name", type="STRING"),
                types.SimpleNamespace(identifier="type", type="ENUM"),
                types.SimpleNamespace(identifier="levels", type="INT"),
                types.SimpleNamespace(identifier="render_levels", type="INT"),
                types.SimpleNamespace(identifier="show_viewport", type="BOOLEAN"),
                types.SimpleNamespace(identifier="show_render", type="BOOLEAN"),
            ]
        ),
    )
    fixture["root"].modifiers.append(modifier)
    addon, _bpy = _load_addon(monkeypatch, all_objects=fixture["all"])
    server = addon.BlenderMCPServer()
    before = server.discover_scene_asset_roots()

    modifier.levels = 4
    after = server.discover_scene_asset_roots()

    assert after["sceneFingerprint"] != before["sceneFingerprint"]


def test_scene_fingerprint_changes_when_constraint_settings_change(monkeypatch):
    fixture = _asset_fixture()
    constraint = fixture["loose"].constraints[0]
    constraint.influence = 1.0
    constraint.bl_rna = types.SimpleNamespace(
        properties=[
            types.SimpleNamespace(identifier="type", type="ENUM"),
            types.SimpleNamespace(identifier="target", type="POINTER"),
            types.SimpleNamespace(identifier="influence", type="FLOAT"),
        ]
    )
    addon, _bpy = _load_addon(monkeypatch, all_objects=fixture["all"])
    server = addon.BlenderMCPServer()
    before = server.discover_scene_asset_roots()

    constraint.influence = 0.25
    after = server.discover_scene_asset_roots()

    assert after["sceneFingerprint"] != before["sceneFingerprint"]


def test_discover_scene_asset_roots_excludes_pole_target_as_dependency(monkeypatch):
    fixture = _asset_fixture()
    pole_target = FakeObject(
        "PoleTarget",
        obj_type="MESH",
        data=FakeMesh("PoleTargetMesh"),
    )
    fixture["loose"].constraints[0].pole_target = pole_target
    fixture["all"].append(pole_target)
    addon, _bpy = _load_addon(monkeypatch, all_objects=fixture["all"])

    result = addon.BlenderMCPServer().discover_scene_asset_roots()

    root_names = {root["objectName"] for root in result["roots"]}
    assert {"Root", "Loose"} <= root_names
    assert "PoleTarget" not in root_names


def test_discover_scene_asset_roots_keeps_armature_parent_as_asset_root(monkeypatch):
    rig = FakeObject("CharacterRig", obj_type="ARMATURE", data=FakeID("CharacterRigData"))
    body = FakeObject("CharacterBody", obj_type="MESH", data=FakeMesh("CharacterBodyMesh"))
    body.parent = rig
    rig.children.append(body)
    body.modifiers.append(types.SimpleNamespace(type="ARMATURE", object=rig))
    addon, _bpy = _load_addon(monkeypatch, all_objects=[rig, body])
    server = addon.BlenderMCPServer()

    result = server.discover_scene_asset_roots()

    assert [root["objectName"] for root in result["roots"]] == ["CharacterRig"]
    assert result["roots"][0]["descendantObjectNames"] == ["CharacterBody"]


def test_explicit_root_snapshot_isolated_and_does_not_change_gui_selection(monkeypatch, tmp_path: Path):
    fixture = _asset_fixture()
    recorded = {}

    def write(filepath, datablocks, **kwargs):
        recorded["filepath"] = filepath
        recorded["datablocks"] = set(datablocks)
        Path(filepath).write_bytes(b"BLENDER-explicit-snapshot")

    addon, bpy = _load_addon(
        monkeypatch,
        selected_objects=[fixture["loose"]],
        all_objects=fixture["all"],
        libraries=types.SimpleNamespace(write=write),
    )
    server = addon.BlenderMCPServer()
    discovery = server.discover_scene_asset_roots()
    before_selection = tuple(bpy.context.selected_objects)
    target = tmp_path / "root-only.blend"

    result = server.create_blend_snapshot(
        filepath=str(target),
        selectionMode="EXPLICIT_ROOTS",
        closureMode="ASSET_CLOSURE",
        rootObjectNames=["Root"],
        expectedSceneFingerprint=discovery["sceneFingerprint"],
    )

    assert tuple(bpy.context.selected_objects) == before_selection
    assert fixture["root"] in recorded["datablocks"]
    assert fixture["child"] in recorded["datablocks"]
    assert fixture["armature"] in recorded["datablocks"]
    assert fixture["target"] not in recorded["datablocks"]
    assert fixture["loose"] not in recorded["datablocks"]
    assert fixture["camera"] not in recorded["datablocks"]
    assert result["rootObjectNames"] == ["Root"]
    assert result["sourceSceneFingerprint"] == discovery["sceneFingerprint"]


def test_explicit_root_snapshot_rejects_stale_scene_fingerprint(monkeypatch, tmp_path: Path):
    fixture = _asset_fixture()
    addon, _bpy = _load_addon(monkeypatch, all_objects=fixture["all"])
    server = addon.BlenderMCPServer()
    discovery = server.discover_scene_asset_roots()
    fixture["loose"].location = (2.0, 0.0, 0.0)

    with pytest.raises(ValueError, match="SCENE_CHANGED"):
        server.create_blend_snapshot(
            filepath=str(tmp_path / "stale.blend"),
            selectionMode="EXPLICIT_ROOTS",
            closureMode="ASSET_CLOSURE",
            rootObjectNames=["Root"],
            expectedSceneFingerprint=discovery["sceneFingerprint"],
        )
    assert not (tmp_path / "stale.blend").exists()

