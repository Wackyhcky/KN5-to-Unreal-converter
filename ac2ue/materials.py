"""Map Assetto Corsa shaders/material properties onto the Unreal master materials.

Masters built by the Unreal script:
  AC_Opaque      Diffuse, Normal, Maps (R spec, G gloss), Detail (masked by diffuse alpha)
  AC_Masked      same, alpha-tested on diffuse alpha, two-sided (fences, trees, grass)
  AC_Translucent same, alpha-blended on diffuse alpha (glass, decals)
  AC_Multilayer  Diffuse x normalised mask-weighted blend of DetailR/G/B/A
                 (ksMultilayer*; a layer can fall back to its mean colour)

This is an approximation: AC uses a Blinn-Phong style pipeline, Unreal is PBR.
Every converted value is exposed as a material-instance parameter you can tweak,
and the original AC values are kept in the manifest.
"""

from __future__ import annotations

import math

# AC texture slot -> (UE parameter name, texture role)
# roles: color (sRGB), normal, mask (linear, TC_Masks)
SLOT_MAP = {
    "txdiffuse": ("Diffuse", "color"),
    "txnormal": ("Normal", "normal"),
    "txmaps": ("Maps", "mask"),
    "txdetail": ("Detail", "color"),
    "txmask": ("Mask", "mask"),
    "txdetailr": ("DetailR", "color"),
    "txdetailg": ("DetailG", "color"),
    "txdetailb": ("DetailB", "color"),
    "txdetaila": ("DetailA", "color"),
}

ROLE_PRIORITY = {"normal": 0, "mask": 1, "color": 2}


def _prop(mat, name, default=None):
    for k, p in mat.properties.items():
        if k.lower() == name.lower():
            return p
    return default


def _a(mat, name, default):
    p = _prop(mat, name)
    return float(p.a) if p is not None else default


def classify(mat) -> str:
    shader = mat.shader.lower()
    slots = {k.lower() for k in mat.mappings}
    if "multilayer" in shader and "txmask" in slots:
        return "Multilayer"
    alpha_ref = _a(mat, "ksAlphaRef", 0.0)
    if mat.blend_mode == 1 and alpha_ref < 0.02:
        return "Translucent"
    # Alpha-blended materials with an alpha reference (fences, foliage cards)
    # are cut-outs in practice; masked keeps their shadows and sorting sane.
    if (mat.blend_mode in (1, 2) or mat.alpha_tested or shader in ("kstree", "ksgrass")
            or "perpixelat" in shader or shader.endswith("_at")):
        return "Masked"
    return "Opaque"


DIFFUSE_REFERENCE = 0.4


def _roughness(spec_exp: float, spec: float) -> float:
    """AC Blinn-Phong -> GGX roughness.

    ksSpecular is an intensity that scales the lobe the same way the exponent
    does, so it is folded into the exponent (ksSpecular 0 = fully matte, as in
    AC). Same arithmetic as the assetto-corsa-gltf project, which measured it
    against real tracks.
    """
    eff = max(spec_exp, 1.0) * max(spec, 0.02)
    return min(max(math.sqrt(2.0 / (eff + 2.0)), 0.04), 1.0)


def convert_material(mat, texture_lookup) -> dict:
    """Return the manifest entry for one KN5 material.

    texture_lookup(original_texture_name, role) -> texture key in the manifest, or None.
    """
    master = classify(mat)
    shader = mat.shader.lower()
    textures = {}
    unmapped = {}
    for slot, tex_name in mat.mappings.items():
        mapping = SLOT_MAP.get(slot.lower())
        if mapping is None:
            unmapped[slot] = tex_name
            continue
        param, role = mapping
        key = texture_lookup(tex_name, role)
        if key is not None:
            textures[param] = key

    spec = _a(mat, "ksSpecular", 0.3)
    # Multilayer road shaders park their sheen in tarmacSpecularMultiplier and
    # only use it where fresnelMaxLevel asks for a reflection.
    if "multilayer" in shader and _a(mat, "fresnelMaxLevel", 0.0) > 0.0:
        spec = max(spec, _a(mat, "tarmacSpecularMultiplier", spec))
    spec_exp = _a(mat, "ksSpecularEXP", 20.0)

    scalars = {
        "Roughness": round(_roughness(spec_exp, spec), 4),
        # UE Specular 0.5 = the standard 4% dielectric reflectance.
        "Specular": round(0.5 * min(max(spec, 0.0), 1.0), 4),
    }
    vectors = {}

    emissive = _prop(mat, "ksEmissive")
    if emissive is not None:
        if any(abs(c) > 1e-6 for c in emissive.c):
            e = [float(c) for c in emissive.c]
        else:
            e = [float(emissive.a)] * 3
        if any(c > 1e-6 for c in e):
            vectors["EmissiveColor"] = e + [1.0]

    # AC lights a surface as albedo * (ksAmbient*ambient + ksDiffuse*sun), so
    # ksDiffuse is in effect a brightness scale. Kunos content sits around 0.4;
    # expressing it relative to that keeps materials' relative brightness (grass
    # shells at 0.19 are meant to be half as bright as the asphalt at 0.42).
    ks_diffuse = _prop(mat, "ksDiffuse")
    if ks_diffuse is not None and ks_diffuse.a > 0:
        # AC shades in gamma space; a gamma-space scale f is f^2.2 in linear.
        f = min(max(ks_diffuse.a / DIFFUSE_REFERENCE, 0.3), 1.5)
        f = round(f ** 2.2, 4)
        vectors["Tint"] = [f, f, f, 1.0]

    alpha_ref = _a(mat, "ksAlphaRef", 0.0)
    clip = alpha_ref if alpha_ref >= 0.02 else None

    if master == "Masked":
        # ksGrass shells are soft, semi-transparent layers in AC; dithering the
        # alpha reproduces that instead of a hard 50% cut-out.
        scalars["Dither"] = 1.0 if shader == "ksgrass" else 0.0

    if master == "Multilayer":
        object_space = "objsp" in shader
        for ch in "RGBA":
            mult = _a(mat, "mult" + ch, 1.0)
            scalars["Mult" + ch] = mult if mult > 0 else 1.0
            # Object-space multilayer projects details from mesh position, and a
            # tiling of 0 collapses a layer onto one texel. In both cases the
            # layer's average colour is the faithful stand-in.
            scalars["UseMean" + ch] = 1.0 if (object_space or mult <= 0) else 0.0
    else:
        # Use the material's own useDetail flag when it has one; otherwise a
        # mapped detail texture means the shader uses it.
        has_detail = "Detail" in textures
        use_detail = _a(mat, "useDetail", 1.0 if has_detail else 0.0)
        scalars["UseDetail"] = 1.0 if (has_detail and use_detail > 0.5) else 0.0
        scalars["DetailTiling"] = _a(mat, "detailUVMultiplier", 1.0) or 1.0

    raw = {name: {"A": p.a, "B": list(p.b), "C": list(p.c), "D": list(p.d)}
           for name, p in mat.properties.items()}

    return {
        "master": master,
        "shader": mat.shader,
        "blend_mode": mat.blend_mode,
        "alpha_tested": mat.alpha_tested,
        "opacity_clip": clip,
        "textures": textures,
        "scalars": scalars,
        "vectors": vectors,
        "unmapped_texture_slots": unmapped,
        "ac_properties": raw,
    }


def texture_roles(materials) -> dict:
    """Collect {texture_name: set(roles)} across all materials of a KN5."""
    roles: dict = {}
    for mat in materials:
        for slot, tex_name in mat.mappings.items():
            mapping = SLOT_MAP.get(slot.lower())
            role = mapping[1] if mapping else "color"
            roles.setdefault(tex_name, set()).add(role)
    return roles
