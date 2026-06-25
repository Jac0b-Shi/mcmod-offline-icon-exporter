#!/usr/bin/env python3
"""Render Minecraft block models into mcmod.cn-sized PNG icons.

This is an offline renderer for documentation icons. It intentionally covers the
JSON model features commonly used by block/item inventory models instead of
trying to reproduce Minecraft's full bake/render pipeline.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import math
import tempfile
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
from PIL import Image


DIRECTIONS = ("north", "east", "south", "west", "up", "down")
FACE_VERTICES = {
    "north": ((1, 0, 0), (0, 0, 0), (0, 1, 0), (1, 1, 0)),
    "south": ((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)),
    "west": ((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)),
    "east": ((1, 0, 1), (1, 0, 0), (1, 1, 0), (1, 1, 1)),
    "up": ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)),
    "down": ((0, 0, 1), (0, 0, 0), (1, 0, 0), (1, 0, 1)),
}


@dataclass(frozen=True)
class ResourceRef:
    namespace: str
    path: str

    @property
    def key(self) -> str:
        return f"{self.namespace}:{self.path}"


@dataclass
class Quad:
    vertices: np.ndarray  # 4x3
    uvs: np.ndarray  # 4x2, in Minecraft 0..16 texture units
    texture: str
    shade: float | None = None


@dataclass
class ModelInstance:
    model_ref: str
    x: float = 0.0
    y: float = 0.0


@dataclass
class RenderResult:
    registry_name: str
    status: str
    message: str = ""
    outputs: list[Path] | None = None
    entry_type: str = "Block"


@dataclass
class CatalogEntry:
    name: str
    englishName: str
    registerName: str
    type: str
    smallIconPath: str = ""
    largeIconPath: str = ""
    renderStatus: str = ""
    renderMessage: str = ""


@dataclass
class OfflineRenderConfig:
    assets_roots: list[Path]
    output_dir: Path
    sizes: list[int] = field(default_factory=lambda: [32, 128])
    namespaces: list[str] = field(default_factory=list)
    include: str = "all"
    only: list[str] = field(default_factory=list)
    limit: int = 0
    canvas_size: int = 512
    margin: float = 0.09
    report_path: Path | None = None
    catalog_jsonl_path: Path | None = None
    catalog_csv_path: Path | None = None


class AssetResolver:
    def __init__(self, roots: Iterable[Path]):
        self.roots = [Path(root).resolve() for root in roots]
        self.model_cache: dict[str, dict[str, Any]] = {}
        self.texture_cache: dict[str, np.ndarray] = {}
        self.texture_image_cache: dict[str, Image.Image] = {}

    def namespace_root(self, namespace: str) -> Path | None:
        roots = self.namespace_roots(namespace)
        return roots[0] if roots else None

    def namespace_roots(self, namespace: str) -> list[Path]:
        roots: list[Path] = []
        for root in self.roots:
            direct = root / namespace
            if direct.exists():
                roots.append(direct)
            if root.name == namespace and self.looks_like_namespace_root(root):
                roots.append(root)
        return roots

    @staticmethod
    def looks_like_namespace_root(path: Path) -> bool:
        return any((path / name).exists() for name in ("blockstates", "models", "textures", "lang"))

    def iter_namespaces(self) -> list[str]:
        namespaces: set[str] = set()
        for root in self.roots:
            if self.looks_like_namespace_root(root):
                namespaces.add(root.name)
                continue
            if not root.exists():
                continue
            for child in root.iterdir():
                if child.is_dir() and self.looks_like_namespace_root(child):
                    namespaces.add(child.name)
        return sorted(namespaces)

    def iter_files(self, namespace: str, *parts: str, suffix: str | None = None) -> list[Path]:
        files: dict[str, Path] = {}
        for ns_root in self.namespace_roots(namespace):
            base = ns_root.joinpath(*parts)
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if not path.is_file():
                    continue
                if suffix and path.suffix != suffix:
                    continue
                key = path.relative_to(base).as_posix()
                files.setdefault(key, path)
        return [files[key] for key in sorted(files)]

    def asset_path(self, namespace: str, *parts: str) -> Path | None:
        for root in self.roots:
            candidates = []
            direct = root / namespace
            if direct.exists():
                candidates.append(direct)
            if root.name == namespace and (root / "models").exists():
                candidates.append(root)
            for ns_root in candidates:
                path = ns_root.joinpath(*parts)
                if path.exists():
                    return path
        return None

    def model_path(self, ref: ResourceRef) -> Path | None:
        return self.asset_path(ref.namespace, "models", f"{ref.path}.json")

    def texture_path(self, ref: ResourceRef) -> Path | None:
        return self.asset_path(ref.namespace, "textures", f"{ref.path}.png")

    def blockstate_path(self, ref: ResourceRef) -> Path | None:
        return self.asset_path(ref.namespace, "blockstates", f"{ref.path}.json")

    def load_json(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def load_model(self, ref: ResourceRef) -> dict[str, Any]:
        key = ref.key
        if key in self.model_cache:
            return self.model_cache[key]

        path = self.model_path(ref)
        if path is None:
            builtin = builtin_model(ref)
            if builtin is None:
                raise FileNotFoundError(f"model not found: {ref.key}")
            raw = builtin
        else:
            raw = self.load_json(path)

        parent_ref = raw.get("parent")
        if parent_ref:
            parent = self.load_model(parse_ref(parent_ref, "minecraft"))
            merged = {
                "textures": dict(parent.get("textures", {})),
                "elements": parent.get("elements"),
                "x_generated_quads": parent.get("x_generated_quads", []),
                "ambientocclusion": parent.get("ambientocclusion", True),
                "render_type": parent.get("render_type"),
            }
            merged["textures"].update(raw.get("textures", {}))
            if "elements" in raw:
                merged["elements"] = raw["elements"]
            if "x_generated_quads" in raw:
                merged["x_generated_quads"] = raw["x_generated_quads"]
            for key_name in ("display", "gui_light"):
                if key_name in raw:
                    merged[key_name] = raw[key_name]
            raw = merged

        self.model_cache[key] = raw
        return raw

    def load_texture(self, ref_string: str, default_namespace: str) -> np.ndarray:
        image = self.load_texture_image(ref_string, default_namespace)
        arr = np.asarray(image, dtype=np.float32) / 255.0
        return arr

    def load_texture_image(self, ref_string: str, default_namespace: str) -> Image.Image:
        ref = parse_ref(ref_string, default_namespace)
        key = ref.key
        if key in self.texture_image_cache:
            return self.texture_image_cache[key].copy()

        path = self.texture_path(ref)
        if path is None:
            img = missing_texture()
        else:
            img = Image.open(path).convert("RGBA")
        img = first_animation_frame(img)
        self.texture_image_cache[key] = img
        return img.copy()

    def has_model(self, namespace: str, model_path: str) -> bool:
        return self.model_path(ResourceRef(namespace, model_path)) is not None


def parse_ref(value: str, default_namespace: str = "minecraft") -> ResourceRef:
    if ":" in value:
        namespace, path = value.split(":", 1)
    else:
        namespace, path = default_namespace, value
    return ResourceRef(namespace, path)


def builtin_model(ref: ResourceRef) -> dict[str, Any] | None:
    if ref.namespace != "minecraft":
        return None

    path = ref.path
    if path in ("item/generated", "item/handheld"):
        return {"textures": {}, "elements": []}
    if path == "block/block":
        return {"textures": {}, "elements": []}
    if path == "block/cube_all":
        return cube_model({"all": "#all"})
    if path == "block/leaves":
        return cube_model({"all": "#all"})
    if path == "block/cube":
        return cube_model(
            {
                "down": "#down",
                "up": "#up",
                "north": "#north",
                "east": "#east",
                "south": "#south",
                "west": "#west",
            }
        )
    if path == "block/cube_bottom_top":
        return cube_model(
            {
                "down": "#bottom",
                "up": "#top",
                "north": "#side",
                "east": "#side",
                "south": "#side",
                "west": "#side",
            }
        )
    if path == "block/cube_column":
        return cube_model(
            {
                "down": "#end",
                "up": "#end",
                "north": "#side",
                "east": "#side",
                "south": "#side",
                "west": "#side",
            }
        )
    if path == "block/template_farmland":
        model = cube_model(
            {
                "down": "#dirt",
                "up": "#top",
                "north": "#side",
                "east": "#side",
                "south": "#side",
                "west": "#side",
            },
            to_y=15,
        )
        return model
    if path == "block/slab":
        return {
            "textures": {},
            "elements": [
                box_element(
                    [0, 0, 0],
                    [16, 8, 16],
                    {
                        "down": "#bottom",
                        "up": "#top",
                        "north": "#side",
                        "east": "#side",
                        "south": "#side",
                        "west": "#side",
                    },
                )
            ],
        }
    if path == "block/slab_top":
        return {
            "textures": {},
            "elements": [
                box_element(
                    [0, 8, 0],
                    [16, 16, 16],
                    {
                        "down": "#bottom",
                        "up": "#top",
                        "north": "#side",
                        "east": "#side",
                        "south": "#side",
                        "west": "#side",
                    },
                )
            ],
        }
    if path in ("block/stairs", "block/inner_stairs", "block/outer_stairs"):
        return stairs_model(path)
    if path == "block/cross":
        return {
            "textures": {"cross": "#cross"},
            "x_generated_quads": [
                {
                    "texture": "#cross",
                    "vertices": [[0, 0, 0], [16, 0, 16], [16, 16, 16], [0, 16, 0]],
                    "uv": [0, 0, 16, 16],
                },
                {
                    "texture": "#cross",
                    "vertices": [[16, 0, 0], [0, 0, 16], [0, 16, 16], [16, 16, 0]],
                    "uv": [0, 0, 16, 16],
                },
            ],
            "elements": [],
        }
    if path == "block/template_single_face":
        return {
            "textures": {"texture": "#texture"},
            "x_generated_quads": [
                {
                    "texture": "#texture",
                    "vertices": [[0, 0.02, 16], [16, 0.02, 16], [16, 0.02, 0], [0, 0.02, 0]],
                    "uv": [0, 0, 16, 16],
                    "shade": 1.0,
                }
            ],
            "elements": [],
        }
    if path in ("block/template_lantern", "block/template_hanging_lantern"):
        return lantern_model(hanging=path.endswith("hanging_lantern"))
    if path in ("block/template_torch", "block/torch"):
        return torch_model(wall=False)
    if path in ("block/template_wall_torch", "block/template_torch_wall", "block/wall_torch"):
        return torch_model(wall=True)
    if path == "block/template_wall_post":
        return {"textures": {"wall": "#wall"}, "elements": [box_element([4, 0, 4], [12, 16, 12], {"all": "#wall"})]}
    if path == "block/template_wall_side":
        return {"textures": {"wall": "#wall"}, "elements": [box_element([5, 0, 0], [11, 14, 8], {"all": "#wall"})]}
    if path == "block/template_wall_side_tall":
        return {"textures": {"wall": "#wall"}, "elements": [box_element([5, 0, 0], [11, 16, 8], {"all": "#wall"})]}
    if path == "block/wall_inventory":
        return {
            "textures": {"wall": "#wall"},
            "elements": [
                box_element([4, 0, 4], [12, 16, 12], {"all": "#wall"}),
                box_element([5, 0, 0], [11, 14, 16], {"all": "#wall"}),
            ],
        }
    if path in (
        "block/template_orientable_trapdoor_bottom",
        "block/template_trapdoor_bottom",
    ):
        return trapdoor_model("bottom")
    if path in (
        "block/template_orientable_trapdoor_top",
        "block/template_trapdoor_top",
    ):
        return trapdoor_model("top")
    if path in (
        "block/template_orientable_trapdoor_open",
        "block/template_trapdoor_open",
    ):
        return trapdoor_model("open")
    return None


def cube_model(face_textures: dict[str, str], to_y: float = 16) -> dict[str, Any]:
    return {
        "textures": {},
        "elements": [box_element([0, 0, 0], [16, to_y, 16], face_textures)],
    }


def box_element(start: list[float], end: list[float], face_textures: dict[str, str]) -> dict[str, Any]:
    faces = {}
    for direction in DIRECTIONS:
        tex = face_textures.get(direction, face_textures.get("all", "#all"))
        faces[direction] = {"uv": [0, 0, 16, 16], "texture": tex}
    return {"from": start, "to": end, "faces": faces}


def stairs_model(path: str) -> dict[str, Any]:
    textures = {
        "down": "#bottom",
        "up": "#top",
        "north": "#side",
        "east": "#side",
        "south": "#side",
        "west": "#side",
    }
    elements = [
        box_element([0, 0, 0], [16, 8, 16], textures),
        box_element([0, 8, 8], [16, 16, 16], textures),
    ]
    if path == "block/inner_stairs":
        elements.append(box_element([8, 8, 0], [16, 16, 8], textures))
    elif path == "block/outer_stairs":
        elements[1] = box_element([8, 8, 8], [16, 16, 16], textures)
    return {"textures": {}, "elements": elements}


def torch_model(wall: bool) -> dict[str, Any]:
    if wall:
        element = box_element([7, 4, 0], [9, 14, 2], {"all": "#torch"})
        element["rotation"] = {"origin": [8, 8, 8], "axis": "x", "angle": -22.5}
    else:
        element = box_element([7, 0, 7], [9, 10, 9], {"all": "#torch"})
    return {"textures": {"torch": "#torch"}, "elements": [element]}


def trapdoor_model(kind: str) -> dict[str, Any]:
    if kind == "top":
        element = box_element([0, 13, 0], [16, 16, 16], {"all": "#texture"})
    elif kind == "open":
        element = box_element([0, 0, 13], [16, 16, 16], {"all": "#texture"})
    else:
        element = box_element([0, 0, 0], [16, 3, 16], {"all": "#texture"})
    return {"textures": {"texture": "#texture"}, "elements": [element]}


def lantern_model(hanging: bool) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    if hanging:
        elements.append(
            {
                "from": [7, 13, 7],
                "to": [9, 16, 9],
                "faces": all_faces("#lantern", [0, 0, 2, 3]),
            }
        )
    else:
        elements.append(
            {
                "from": [6, 0, 6],
                "to": [10, 2, 10],
                "faces": all_faces("#lantern", [0, 0, 4, 2]),
            }
        )
    elements.append(
        {
            "from": [5, 2, 5],
            "to": [11, 10, 11],
            "faces": all_faces("#lantern", [0, 0, 6, 8]),
        }
    )
    return {"textures": {"lantern": "#lantern"}, "elements": elements}


def all_faces(texture: str, uv: list[float]) -> dict[str, dict[str, Any]]:
    return {direction: {"texture": texture, "uv": uv} for direction in DIRECTIONS}


def missing_texture() -> Image.Image:
    img = Image.new("RGBA", (16, 16), (0, 0, 0, 255))
    px = img.load()
    for y in range(16):
        for x in range(16):
            if ((x // 4) + (y // 4)) % 2 == 0:
                px[x, y] = (255, 0, 255, 255)
            else:
                px[x, y] = (0, 0, 0, 255)
    return img


def first_animation_frame(image: Image.Image) -> Image.Image:
    if image.height > image.width and image.height % image.width == 0:
        return image.crop((0, 0, image.width, image.width))
    return image


def resolve_texture(texture_token: str, textures: dict[str, str]) -> str:
    value = texture_token
    seen: set[str] = set()
    while value.startswith("#"):
        key = value[1:]
        if key in seen:
            return "minecraft:block/missingno"
        seen.add(key)
        value = textures.get(key)
        if not value or value == "#missing":
            return "minecraft:block/missingno"
    return value


def build_model_quads(
    resolver: AssetResolver,
    instance: ModelInstance,
    default_namespace: str,
) -> list[Quad]:
    ref = parse_model_ref(instance.model_ref, default_namespace, resolver)
    model = resolver.load_model(ref)
    textures = model.get("textures", {})
    quads: list[Quad] = []

    for raw_quad in model.get("x_generated_quads", []):
        texture = resolve_texture(raw_quad["texture"], textures)
        uv = uv_corners(raw_quad.get("uv", [0, 0, 16, 16]), 0)
        quads.append(
            Quad(
                np.asarray(raw_quad["vertices"], dtype=np.float32),
                uv,
                texture,
                raw_quad.get("shade"),
            )
        )

    for element in model.get("elements") or []:
        quads.extend(element_quads(element, textures))

    if instance.x:
        quads = [rotate_quad(q, [8, 8, 8], "x", instance.x) for q in quads]
    if instance.y:
        quads = [rotate_quad(q, [8, 8, 8], "y", instance.y) for q in quads]
    return quads


def parse_model_ref(value: str, default_namespace: str, resolver: AssetResolver) -> ResourceRef:
    if ":" in value:
        return parse_ref(value)
    if resolver.has_model(default_namespace, value):
        return ResourceRef(default_namespace, value)
    return parse_ref(value, "minecraft")


def element_quads(element: dict[str, Any], textures: dict[str, str]) -> list[Quad]:
    start = np.asarray(element.get("from", [0, 0, 0]), dtype=np.float32)
    end = np.asarray(element.get("to", [16, 16, 16]), dtype=np.float32)
    faces = element.get("faces") or {}
    result: list[Quad] = []
    rotation = element.get("rotation")

    for direction, face in faces.items():
        if direction not in FACE_VERTICES:
            continue
        vertices = []
        for bit in FACE_VERTICES[direction]:
            vertices.append(
                [
                    end[0] if bit[0] else start[0],
                    end[1] if bit[1] else start[1],
                    end[2] if bit[2] else start[2],
                ]
            )
        vertices_arr = np.asarray(vertices, dtype=np.float32)
        if rotation:
            vertices_arr = rotate_points(
                vertices_arr,
                rotation.get("origin", [8, 8, 8]),
                rotation.get("axis", "y"),
                float(rotation.get("angle", 0)),
            )
        uv = face.get("uv") or default_uv(direction, start, end)
        texture = resolve_texture(face.get("texture", "#missing"), textures)
        result.append(Quad(vertices_arr, uv_corners(uv, int(face.get("rotation", 0))), texture))
    return result


def default_uv(direction: str, start: np.ndarray, end: np.ndarray) -> list[float]:
    x1, y1, z1 = start.tolist()
    x2, y2, z2 = end.tolist()
    if direction in ("up", "down"):
        return [x1, z1, x2, z2]
    if direction in ("north", "south"):
        return [x1, 16 - y2, x2, 16 - y1]
    return [z1, 16 - y2, z2, 16 - y1]


def uv_corners(uv: Iterable[float], rotation: int = 0) -> np.ndarray:
    u1, v1, u2, v2 = [float(v) for v in uv]
    corners = np.asarray([[u1, v2], [u2, v2], [u2, v1], [u1, v1]], dtype=np.float32)
    steps = (rotation // 90) % 4
    if steps:
        corners = np.roll(corners, -steps, axis=0)
    return corners


def rotate_quad(quad: Quad, origin: Iterable[float], axis: str, angle: float) -> Quad:
    return Quad(rotate_points(quad.vertices, origin, axis, angle), quad.uvs.copy(), quad.texture, quad.shade)


def rotate_points(points: np.ndarray, origin: Iterable[float], axis: str, angle: float) -> np.ndarray:
    radians = math.radians(angle)
    c = math.cos(radians)
    s = math.sin(radians)
    if axis == "x":
        matrix = np.asarray([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float32)
    elif axis == "z":
        matrix = np.asarray([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float32)
    else:
        matrix = np.asarray([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float32)
    origin_arr = np.asarray(origin, dtype=np.float32)
    return (points - origin_arr) @ matrix.T + origin_arr


def model_instances_for_block(
    resolver: AssetResolver,
    registry_name: str,
) -> list[ModelInstance]:
    block_ref = parse_ref(registry_name)
    blockstate_path = resolver.blockstate_path(block_ref)
    if blockstate_path is None:
        return [ModelInstance(f"{block_ref.namespace}:block/{block_ref.path}")]

    blockstate = resolver.load_json(blockstate_path)
    if "variants" in blockstate:
        variant = choose_variant(blockstate["variants"])
        return normalize_apply(variant)

    if "multipart" in blockstate:
        selected: list[ModelInstance] = []
        first_apply: Any | None = None
        for part in blockstate["multipart"]:
            apply = part.get("apply")
            if first_apply is None:
                first_apply = apply
            if matches_default_state(part.get("when")):
                selected.extend(normalize_apply(apply))
        if not selected and first_apply is not None:
            selected.extend(normalize_apply(first_apply))
        return selected

    return [ModelInstance(f"{block_ref.namespace}:block/{block_ref.path}")]


def choose_variant(variants: dict[str, Any]) -> Any:
    preferred = ("", "facing=north", "axis=y", "lit=false", "waterlogged=false")
    for key in preferred:
        if key in variants:
            return first_variant_entry(variants[key])
    for key in sorted(variants.keys()):
        return first_variant_entry(variants[key])
    raise ValueError("empty variants")


def first_variant_entry(value: Any) -> Any:
    if isinstance(value, list):
        return value[0]
    return value


def normalize_apply(value: Any) -> list[ModelInstance]:
    if value is None:
        return []
    if isinstance(value, list):
        result: list[ModelInstance] = []
        for item in value:
            result.extend(normalize_apply(item))
        return result
    if "model" not in value:
        return []
    return [
        ModelInstance(
            model_ref=value["model"],
            x=float(value.get("x", 0)),
            y=float(value.get("y", 0)),
        )
    ]


def matches_default_state(condition: Any) -> bool:
    if condition is None:
        return True
    if isinstance(condition, list):
        return any(matches_default_state(item) for item in condition)
    if not isinstance(condition, dict):
        return True
    if "OR" in condition:
        return any(matches_default_state(item) for item in condition["OR"])
    if "AND" in condition:
        return all(matches_default_state(item) for item in condition["AND"])

    defaults = {
        "north": "false",
        "east": "false",
        "south": "false",
        "west": "false",
        "up": "false",
        "down": "false",
        "x": "false",
        "y": "true",
        "z": "false",
        "top": "false",
        "bottom": "false",
        "facing": "north",
        "horizontal_facing": "north",
        "axis": "y",
        "lit": "false",
        "waterlogged": "false",
        "powered": "false",
        "open": "false",
    }
    for key, expected in condition.items():
        if key in ("OR", "AND"):
            continue
        actual = defaults.get(key, "false")
        choices = [str(v).strip() for v in str(expected).split("|")]
        if actual not in choices:
            return False
    return True


def render_quads(
    resolver: AssetResolver,
    quads: list[Quad],
    default_namespace: str,
    canvas_size: int,
    margin: float,
) -> Image.Image:
    if not quads:
        raise ValueError("model has no renderable quads")

    view = view_basis()
    projected: list[np.ndarray] = []
    depths: list[np.ndarray] = []
    for quad in quads:
        coords = project_points(quad.vertices - 8.0, view)
        projected.append(coords[:, :2])
        depths.append(coords[:, 2])

    all_points = np.vstack(projected)
    min_xy = all_points.min(axis=0)
    max_xy = all_points.max(axis=0)
    span = np.maximum(max_xy - min_xy, 1e-4)
    scale = canvas_size * (1.0 - margin * 2.0) / float(max(span[0], span[1]))
    center = (min_xy + max_xy) / 2.0

    projected = [(points - center) * scale + canvas_size / 2.0 for points in projected]

    color = np.zeros((canvas_size, canvas_size, 4), dtype=np.float32)
    zbuf = np.full((canvas_size, canvas_size), -1e9, dtype=np.float32)
    order = sorted(range(len(quads)), key=lambda i: float(np.mean(depths[i])))

    for index in order:
        quad = quads[index]
        texture = resolver.load_texture(quad.texture, default_namespace)
        shade = quad.shade if quad.shade is not None else quad_shade(quad.vertices)
        render_quad(color, zbuf, projected[index], depths[index], quad.uvs, texture, shade)

    arr = np.clip(color * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def view_basis() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    forward = normalize(np.asarray([1.0, 1.0, 1.0], dtype=np.float32))
    right = normalize(np.asarray([1.0, 0.0, -1.0], dtype=np.float32))
    up = normalize(np.cross(forward, right))
    return right, up, forward


def project_points(points: np.ndarray, basis: tuple[np.ndarray, np.ndarray, np.ndarray]) -> np.ndarray:
    right, up, forward = basis
    x = points @ right
    y = -(points @ up)
    z = points @ forward
    return np.stack([x, y, z], axis=1)


def normalize(value: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(value))
    if norm <= 1e-8:
        return value
    return value / norm


def quad_shade(vertices: np.ndarray) -> float:
    edge_a = vertices[1] - vertices[0]
    edge_b = vertices[2] - vertices[1]
    normal = normalize(np.cross(edge_a, edge_b))
    light_dir = normalize(np.asarray([-0.4, 0.9, -0.65], dtype=np.float32))
    direct = max(float(np.dot(normal, light_dir)), 0.0)
    vertical_bonus = 0.14 * max(float(normal[1]), 0.0)
    shade = 0.50 + direct * 0.48 + vertical_bonus
    return float(max(0.42, min(1.08, shade)))


def render_quad(
    color: np.ndarray,
    zbuf: np.ndarray,
    points: np.ndarray,
    depth: np.ndarray,
    uvs: np.ndarray,
    texture: np.ndarray,
    shade: float,
) -> None:
    draw_triangle(color, zbuf, points[[0, 1, 2]], depth[[0, 1, 2]], uvs[[0, 1, 2]], texture, shade)
    draw_triangle(color, zbuf, points[[0, 2, 3]], depth[[0, 2, 3]], uvs[[0, 2, 3]], texture, shade)


def draw_triangle(
    color: np.ndarray,
    zbuf: np.ndarray,
    points: np.ndarray,
    depth: np.ndarray,
    uvs: np.ndarray,
    texture: np.ndarray,
    shade: float,
) -> None:
    height, width = zbuf.shape
    min_x = max(int(math.floor(points[:, 0].min())), 0)
    max_x = min(int(math.ceil(points[:, 0].max())), width - 1)
    min_y = max(int(math.floor(points[:, 1].min())), 0)
    max_y = min(int(math.ceil(points[:, 1].max())), height - 1)
    if min_x > max_x or min_y > max_y:
        return

    p0, p1, p2 = points
    denom = (p1[1] - p2[1]) * (p0[0] - p2[0]) + (p2[0] - p1[0]) * (p0[1] - p2[1])
    if abs(float(denom)) < 1e-6:
        return

    xs = np.arange(min_x, max_x + 1, dtype=np.float32) + 0.5
    ys = np.arange(min_y, max_y + 1, dtype=np.float32) + 0.5
    grid_x, grid_y = np.meshgrid(xs, ys)
    w0 = ((p1[1] - p2[1]) * (grid_x - p2[0]) + (p2[0] - p1[0]) * (grid_y - p2[1])) / denom
    w1 = ((p2[1] - p0[1]) * (grid_x - p2[0]) + (p0[0] - p2[0]) * (grid_y - p2[1])) / denom
    w2 = 1.0 - w0 - w1
    mask = (w0 >= -1e-4) & (w1 >= -1e-4) & (w2 >= -1e-4)
    if not np.any(mask):
        return

    tri_depth = w0 * depth[0] + w1 * depth[1] + w2 * depth[2]
    target_z = zbuf[min_y : max_y + 1, min_x : max_x + 1]
    mask &= tri_depth >= target_z
    if not np.any(mask):
        return

    uv = w0[..., None] * uvs[0] + w1[..., None] * uvs[1] + w2[..., None] * uvs[2]
    tex_h, tex_w = texture.shape[:2]
    sample_x = np.clip(np.floor((uv[..., 0] / 16.0) * tex_w).astype(np.int32), 0, tex_w - 1)
    sample_y = np.clip(np.floor((uv[..., 1] / 16.0) * tex_h).astype(np.int32), 0, tex_h - 1)
    sampled = texture[sample_y, sample_x].copy()
    sampled[..., :3] *= shade

    alpha_mask = sampled[..., 3] > 0.01
    mask &= alpha_mask
    if not np.any(mask):
        return

    target = color[min_y : max_y + 1, min_x : max_x + 1]
    src = sampled[mask]
    dst = target[mask]
    alpha = src[:, 3:4]
    out_rgb = src[:, :3] * alpha + dst[:, :3] * (1.0 - alpha)
    out_a = alpha[:, 0] + dst[:, 3] * (1.0 - alpha[:, 0])
    dst[:, :3] = out_rgb
    dst[:, 3] = out_a
    target[mask] = dst
    target_z[mask] = tri_depth[mask]


def load_jsonl_entries(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL line: {exc}") from exc
    return entries


def discover_source_asset_roots(source_root: Path) -> list[Path]:
    if source_root.name == "assets" and source_root.exists():
        return [source_root.resolve()]
    if source_root.exists() and AssetResolver.looks_like_namespace_root(source_root):
        return [source_root.resolve()]
    candidates = [
        source_root / "src" / "main" / "resources" / "assets",
        source_root / "src" / "generated" / "assets",
        source_root / "src" / "generated" / "resources" / "assets",
        source_root / "build" / "resources" / "main" / "assets",
    ]
    return [path.resolve() for path in candidates if path.exists()]


def extract_archive_assets(archive_path: Path, temp_root: Path) -> Path:
    assets_root = temp_root / "assets"
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.startswith("assets/"):
                continue
            target = temp_root / Path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
    return assets_root


def load_lang(resolver: AssetResolver, namespace: str, locale: str) -> dict[str, str]:
    path = resolver.asset_path(namespace, "lang", f"{locale}.json")
    if path is None:
        return {}
    data = resolver.load_json(path)
    return {str(key): str(value) for key, value in data.items()}


def lang_ids(data: dict[str, str], namespace: str, kind: str) -> set[str]:
    prefix = f"{kind}.{namespace}."
    ids: set[str] = set()
    for key in data:
        if not key.startswith(prefix):
            continue
        tail = key[len(prefix) :]
        if not tail:
            continue
        ids.add(tail.split(".", 1)[0])
    return ids


def asset_ids(resolver: AssetResolver, namespace: str, *parts: str, suffix: str) -> set[str]:
    ids: set[str] = set()
    for ns_root in resolver.namespace_roots(namespace):
        base = ns_root.joinpath(*parts)
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix != suffix:
                continue
            rel = path.relative_to(base).with_suffix("").as_posix()
            if "/" not in rel:
                ids.add(rel)
    return ids


def humanize_id(identifier: str) -> str:
    return identifier.rsplit("/", 1)[-1].replace("_", " ").strip().title() or identifier


def localized_name(
    zh_lang: dict[str, str],
    en_lang: dict[str, str],
    namespace: str,
    kind: str,
    identifier: str,
) -> tuple[str, str]:
    key = f"{kind}.{namespace}.{identifier}"
    fallback = humanize_id(identifier)
    return zh_lang.get(key, fallback), en_lang.get(key, fallback)


def scan_offline_catalog(resolver: AssetResolver, namespaces: Iterable[str] | None = None) -> list[CatalogEntry]:
    selected_namespaces = sorted(set(namespaces or resolver.iter_namespaces()))
    entries: list[CatalogEntry] = []
    for namespace in selected_namespaces:
        zh_lang = load_lang(resolver, namespace, "zh_cn")
        en_lang = load_lang(resolver, namespace, "en_us")

        block_lang_ids = set()
        block_lang_ids.update(lang_ids(zh_lang, namespace, "block"))
        block_lang_ids.update(lang_ids(en_lang, namespace, "block"))
        blockstate_ids = asset_ids(resolver, namespace, "blockstates", suffix=".json")
        block_model_ids = asset_ids(resolver, namespace, "models", "block", suffix=".json")
        block_ids = set(blockstate_ids)
        block_ids.update(identifier for identifier in block_lang_ids if identifier in blockstate_ids or identifier in block_model_ids)

        item_lang_ids = set()
        item_lang_ids.update(lang_ids(zh_lang, namespace, "item"))
        item_lang_ids.update(lang_ids(en_lang, namespace, "item"))
        item_model_ids = asset_ids(resolver, namespace, "models", "item", suffix=".json")
        if item_lang_ids:
            item_ids = set(item_lang_ids)
        else:
            item_ids = set(item_model_ids)
        item_ids.difference_update(block_ids)

        for identifier in sorted(block_ids):
            name, english_name = localized_name(zh_lang, en_lang, namespace, "block", identifier)
            entries.append(CatalogEntry(name, english_name, f"{namespace}:{identifier}", "Block"))
        for identifier in sorted(item_ids):
            name, english_name = localized_name(zh_lang, en_lang, namespace, "item", identifier)
            entries.append(CatalogEntry(name, english_name, f"{namespace}:{identifier}", "Item"))
    return sorted(entries, key=lambda entry: (entry.registerName, entry.type))


def render_entry(
    resolver: AssetResolver,
    entry: dict[str, Any],
    output_dir: Path,
    sizes: list[int],
    canvas_size: int,
    margin: float,
    fallback_icons: bool,
) -> RenderResult:
    registry_name = entry.get("registerName", "")
    if entry.get("type") != "Block":
        return RenderResult(registry_name, "skip", "not a block")

    namespace = parse_ref(registry_name).namespace
    try:
        instances = model_instances_for_block(resolver, registry_name)
        quads: list[Quad] = []
        for instance in instances:
            quads.extend(build_model_quads(resolver, instance, namespace))
        item_error: Exception | None = None
        if not quads:
            try:
                block_ref = parse_ref(registry_name)
                quads.extend(build_model_quads(resolver, ModelInstance(f"{block_ref.namespace}:item/{block_ref.path}"), namespace))
            except Exception as exc:  # noqa: BLE001
                item_error = exc
        if not quads:
            particle_image = particle_image_for_instances(resolver, instances, namespace)
            if particle_image is not None:
                outputs = save_icon_set(particle_image, registry_name, output_dir, sizes, resample=Image.Resampling.NEAREST)
                return RenderResult(registry_name, "rendered", "used particle texture", outputs, "Block")
        if not quads and item_error is not None:
            raise item_error
        image = render_quads(resolver, quads, namespace, canvas_size, margin)
        outputs = save_icon_set(image, registry_name, output_dir, sizes)
        return RenderResult(registry_name, "rendered", outputs=outputs)
    except Exception as exc:  # noqa: BLE001 - CLI report should include per-entry failure.
        if fallback_icons:
            try:
                outputs = save_fallback_icons(entry, output_dir, sizes)
                return RenderResult(registry_name, "fallback", str(exc), outputs)
            except Exception as fallback_exc:  # noqa: BLE001
                return RenderResult(registry_name, "failed", f"{exc}; fallback failed: {fallback_exc}")
        return RenderResult(registry_name, "failed", str(exc))


def render_block_registry(
    resolver: AssetResolver,
    registry_name: str,
    output_dir: Path,
    sizes: list[int],
    canvas_size: int,
    margin: float,
) -> RenderResult:
    namespace = parse_ref(registry_name).namespace
    try:
        instances = model_instances_for_block(resolver, registry_name)
        quads: list[Quad] = []
        for instance in instances:
            quads.extend(build_model_quads(resolver, instance, namespace))
        item_error: Exception | None = None
        if not quads:
            try:
                block_ref = parse_ref(registry_name)
                quads.extend(build_model_quads(resolver, ModelInstance(f"{block_ref.namespace}:item/{block_ref.path}"), namespace))
            except Exception as exc:  # noqa: BLE001
                item_error = exc
        if not quads:
            particle_image = particle_image_for_instances(resolver, instances, namespace)
            if particle_image is not None:
                outputs = save_icon_set(particle_image, registry_name, output_dir, sizes, resample=Image.Resampling.NEAREST)
                return RenderResult(registry_name, "rendered", "used particle texture", outputs, "Block")
        if not quads and item_error is not None:
            raise item_error
        image = render_quads(resolver, quads, namespace, canvas_size, margin)
        outputs = save_icon_set(image, registry_name, output_dir, sizes)
        return RenderResult(registry_name, "rendered", outputs=outputs, entry_type="Block")
    except Exception as exc:  # noqa: BLE001
        return RenderResult(registry_name, "failed", str(exc), entry_type="Block")


def particle_image_for_instances(
    resolver: AssetResolver,
    instances: list[ModelInstance],
    default_namespace: str,
) -> Image.Image | None:
    for instance in instances:
        try:
            ref = parse_model_ref(instance.model_ref, default_namespace, resolver)
            model = resolver.load_model(ref)
        except Exception:  # noqa: BLE001
            continue
        textures = model.get("textures", {})
        particle = textures.get("particle")
        if particle:
            texture = resolve_texture(particle, textures)
            return resolver.load_texture_image(texture, default_namespace)
    return None


def render_item_registry(
    resolver: AssetResolver,
    registry_name: str,
    output_dir: Path,
    sizes: list[int],
    canvas_size: int,
    margin: float,
) -> RenderResult:
    ref = parse_ref(registry_name)
    try:
        image = render_item_image(resolver, ref, canvas_size, margin)
        outputs = save_icon_set(image, registry_name, output_dir, sizes, resample=Image.Resampling.NEAREST)
        return RenderResult(registry_name, "rendered", outputs=outputs, entry_type="Item")
    except Exception as exc:  # noqa: BLE001
        return RenderResult(registry_name, "failed", str(exc), entry_type="Item")


def render_item_image(resolver: AssetResolver, ref: ResourceRef, canvas_size: int, margin: float) -> Image.Image:
    model_ref = ResourceRef(ref.namespace, f"item/{ref.path}")
    try:
        model = resolver.load_model(model_ref)
    except FileNotFoundError:
        texture_ref = ResourceRef(ref.namespace, f"item/{ref.path}")
        if resolver.texture_path(texture_ref) is None:
            raise
        return resolver.load_texture_image(texture_ref.key, ref.namespace)

    quads = build_model_quads(resolver, ModelInstance(model_ref.key), ref.namespace)
    if quads:
        return render_quads(resolver, quads, ref.namespace, canvas_size, margin)

    textures = model.get("textures", {})
    layers = sorted(
        (key for key in textures if key.startswith("layer") and key[5:].isdigit()),
        key=lambda key: int(key[5:]),
    )
    if layers:
        return compose_item_layers(resolver, textures, layers, ref.namespace)

    texture_ref = ResourceRef(ref.namespace, f"item/{ref.path}")
    if resolver.texture_path(texture_ref) is not None:
        return resolver.load_texture_image(texture_ref.key, ref.namespace)
    raise ValueError("item has no renderable model layers or direct texture")


def compose_item_layers(
    resolver: AssetResolver,
    textures: dict[str, str],
    layers: list[str],
    default_namespace: str,
) -> Image.Image:
    images: list[Image.Image] = []
    for layer in layers:
        texture = resolve_texture(textures[layer], textures)
        images.append(resolver.load_texture_image(texture, default_namespace).convert("RGBA"))
    if not images:
        raise ValueError("item model has no texture layers")
    width = max(image.width for image in images)
    height = max(image.height for image in images)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for image in images:
        if image.size != canvas.size:
            image = image.resize(canvas.size, Image.Resampling.NEAREST)
        canvas.alpha_composite(image)
    return canvas


def render_catalog_entry(
    resolver: AssetResolver,
    entry: CatalogEntry,
    output_dir: Path,
    sizes: list[int],
    canvas_size: int,
    margin: float,
) -> RenderResult:
    if entry.type == "Block":
        return render_block_registry(resolver, entry.registerName, output_dir, sizes, canvas_size, margin)
    return render_item_registry(resolver, entry.registerName, output_dir, sizes, canvas_size, margin)


def update_catalog_entry_paths(entry: CatalogEntry, result: RenderResult) -> None:
    entry.renderStatus = result.status
    entry.renderMessage = result.message
    outputs = sorted(result.outputs or [], key=output_size)
    if outputs:
        entry.smallIconPath = str(outputs[0])
        entry.largeIconPath = str(outputs[-1])


def output_size(path: Path) -> int:
    stem = path.stem
    if "_" not in stem or not stem.endswith("x"):
        return 0
    tail = stem.rsplit("_", 1)[-1][:-1]
    return int(tail) if tail.isdigit() else 0


def run_offline_job(
    config: OfflineRenderConfig,
    on_result: Callable[[RenderResult, int, int], None] | None = None,
) -> tuple[list[CatalogEntry], list[RenderResult]]:
    resolver = AssetResolver(config.assets_roots)
    entries = scan_offline_catalog(resolver, config.namespaces)
    only = set(config.only)
    if only:
        entries = [entry for entry in entries if entry.registerName in only]
    if config.include == "blocks":
        entries = [entry for entry in entries if entry.type == "Block"]
    elif config.include == "items":
        entries = [entry for entry in entries if entry.type == "Item"]
    if config.limit:
        entries = entries[: config.limit]

    results: list[RenderResult] = []
    total = len(entries)
    for index, entry in enumerate(entries, 1):
        result = render_catalog_entry(
            resolver,
            entry,
            config.output_dir,
            config.sizes,
            config.canvas_size,
            config.margin,
        )
        update_catalog_entry_paths(entry, result)
        results.append(result)
        if on_result:
            on_result(result, index, total)

    report_path = config.report_path or (config.output_dir / "report.json")
    catalog_jsonl_path = config.catalog_jsonl_path or (config.output_dir / "catalog.jsonl")
    catalog_csv_path = config.catalog_csv_path or (config.output_dir / "catalog.csv")
    write_report(results, report_path)
    write_catalog(entries, catalog_jsonl_path, catalog_csv_path)
    return entries, results


def write_report(results: list[RenderResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "registryName": item.registry_name,
                    "type": item.entry_type,
                    "status": item.status,
                    "message": item.message,
                    "outputs": [str(p) for p in (item.outputs or [])],
                }
                for item in results
            ],
            f,
            ensure_ascii=False,
            indent=2,
        )


def write_catalog(entries: list[CatalogEntry], jsonl_path: Path, csv_path: Path) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "name",
        "englishName",
        "registerName",
        "type",
        "smallIconPath",
        "largeIconPath",
        "renderStatus",
        "renderMessage",
    ]
    with jsonl_path.open("w", encoding="utf-8", newline="") as f:
        for entry in entries:
            json.dump({field: getattr(entry, field) for field in fieldnames}, f, ensure_ascii=False)
            f.write("\n")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for entry in entries:
            writer.writerow({field: getattr(entry, field) for field in fieldnames})


def save_icon_set(
    image: Image.Image,
    registry_name: str,
    output_dir: Path,
    sizes: list[int],
    resample: Image.Resampling = Image.Resampling.LANCZOS,
) -> list[Path]:
    paths: list[Path] = []
    for size in sizes:
        resized = image.resize((size, size), resample)
        path = output_path(output_dir, registry_name, size)
        path.parent.mkdir(parents=True, exist_ok=True)
        resized.save(path)
        paths.append(path)
    return paths


def save_fallback_icons(entry: dict[str, Any], output_dir: Path, sizes: list[int]) -> list[Path]:
    outputs: list[Path] = []
    registry_name = entry["registerName"]
    field_by_size = {32: "smallIcon", 128: "largeIcon", 36: "smallIcon", 144: "largeIcon"}
    for size in sizes:
        field = field_by_size.get(size, "largeIcon")
        raw = entry.get(field) or entry.get("largeIcon") or entry.get("smallIcon")
        if not raw:
            raise ValueError("no fallback icon field")
        data = raw.split(",", 1)[1] if raw.startswith("data:") else raw
        image = Image.open(io.BytesIO(base64.b64decode(data))).convert("RGBA")
        if image.size != (size, size):
            image = image.resize((size, size), Image.Resampling.LANCZOS)
        path = output_path(output_dir, registry_name, size)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        outputs.append(path)
    return outputs


def output_path(output_dir: Path, registry_name: str, size: int) -> Path:
    ref = parse_ref(registry_name)
    return output_dir / ref.namespace / f"{ref.path}_{size}x.png"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Optional LetMeSeeSee mcmod JSONL export for legacy mode.")
    parser.add_argument("--source", type=Path, help="Mod source/project directory, assets directory, or namespace assets root.")
    parser.add_argument("--jar", type=Path, help="Built mod JAR/ZIP. assets/** will be read offline.")
    parser.add_argument(
        "--assets-root",
        action="append",
        default=[],
        type=Path,
        help="Extra resources/assets directory. Can be passed multiple times.",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output directory for generated icons.")
    parser.add_argument("--sizes", nargs="+", type=int, default=[32, 128], help="Icon sizes to write.")
    parser.add_argument("--only", action="append", default=[], help="Render only this registry name. Repeatable.")
    parser.add_argument("--namespace", action="append", default=[], help="Render only this namespace. Repeatable.")
    parser.add_argument("--modid", action="append", default=[], help="Offline mode namespace filter. Repeatable.")
    parser.add_argument("--include", choices=("all", "blocks", "items"), default="all", help="Offline export scope.")
    parser.add_argument("--limit", type=int, default=0, help="Limit processed Block entries, useful for tests.")
    parser.add_argument("--canvas-size", type=int, default=512, help="Internal render canvas before resizing.")
    parser.add_argument("--margin", type=float, default=0.09, help="Transparent margin ratio around projected model.")
    parser.add_argument("--fallback-icons", action="store_true", help="Use LetMeSeeSee base64 icons when render fails.")
    parser.add_argument("--report", type=Path, help="Optional JSON report path.")
    parser.add_argument("--catalog-jsonl", type=Path, help="Offline mode catalog JSONL path.")
    parser.add_argument("--catalog-csv", type=Path, help="Offline mode catalog CSV path.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.source or args.jar:
        return run_offline_cli(args)

    if not args.input:
        print("error: provide --source/--jar for offline mode, or --input with --assets-root for legacy JSONL mode", file=sys.stderr)
        return 2
    if not args.assets_root:
        print("error: legacy JSONL mode requires at least one --assets-root", file=sys.stderr)
        return 2

    resolver = AssetResolver(args.assets_root)
    entries = load_jsonl_entries(args.input)

    only = set(args.only)
    namespaces = set(args.namespace)
    selected: list[dict[str, Any]] = []
    for entry in entries:
        registry_name = entry.get("registerName", "")
        if entry.get("type") != "Block":
            continue
        if only and registry_name not in only:
            continue
        if namespaces and parse_ref(registry_name).namespace not in namespaces:
            continue
        selected.append(entry)
        if args.limit and len(selected) >= args.limit:
            break

    results: list[RenderResult] = []
    for entry in selected:
        result = render_entry(
            resolver,
            entry,
            args.out,
            args.sizes,
            args.canvas_size,
            args.margin,
            args.fallback_icons,
        )
        results.append(result)
        suffix = f": {result.message}" if result.message else ""
        print(f"{result.status:8s} {result.registry_name}{suffix}")

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print("summary " + " ".join(f"{key}={counts[key]}" for key in sorted(counts)))

    if args.report:
        write_report(results, args.report)
    return 0 if not counts.get("failed") else 2


def run_offline_cli(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="mcmod-icons-") as temp_dir:
        temp_root = Path(temp_dir)
        roots: list[Path] = []
        if args.source:
            roots.extend(discover_source_asset_roots(args.source.resolve()))
        if args.jar:
            roots.append(extract_archive_assets(args.jar.resolve(), temp_root))
        roots.extend(path.resolve() for path in args.assets_root)
        roots = [path for path in roots if path.exists()]
        if not roots:
            print("error: no usable assets roots discovered", file=sys.stderr)
            return 2

        namespaces = args.modid or args.namespace
        config = OfflineRenderConfig(
            assets_roots=roots,
            output_dir=args.out,
            sizes=args.sizes,
            namespaces=namespaces,
            include=args.include,
            only=args.only,
            limit=args.limit,
            canvas_size=args.canvas_size,
            margin=args.margin,
            report_path=args.report,
            catalog_jsonl_path=args.catalog_jsonl,
            catalog_csv_path=args.catalog_csv,
        )
        entries, results = run_offline_job(config)
        for result in results:
            suffix = f": {result.message}" if result.message else ""
            print(f"{result.status:8s} {result.entry_type:5s} {result.registry_name}{suffix}")
        counts: dict[str, int] = {}
        for result in results:
            counts[result.status] = counts.get(result.status, 0) + 1
        print("summary " + " ".join(f"{key}={counts[key]}" for key in sorted(counts)))
        print(f"catalog entries={len(entries)} output={args.out}")
        return 0


def cli() -> None:
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
