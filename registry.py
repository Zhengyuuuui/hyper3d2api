"""Hyper3D model registry — verified 2026-10-08 against the live API.

Key facts baked in from reverse-engineering (see session RECON.md §6):
  * server provider enum is 12 values; `sora2` / `luma` are NOT in it
    -> the bundle's hidden Sora2/Luma models are front-end placeholders and
       cannot be called at all.
  * provider <-> model binding is validated server-side; spoofing is rejected.
  * paid models fail with `NEED_ADVANCED_SUBSCRIPTION` (server-side wall).
  * `text_to_image` also posts to /generate_from_text (JSON, params.size="WxH").
  * Pro tier == api string gets a `/pro` suffix.
  * luma/veo templates set durationAddSuffix -> duration becomes "8s" (string).
"""

# ---------------------------------------------------------------------------
# 3D generation — SEPARATE API surface (`/api/v2/*`, not `/api/gateway/*`)
# ---------------------------------------------------------------------------
# The web session JWT does NOT work here: every /api/v2/* call returns
#   401 {"error":"UNAUTHORIZED","message":"Active API key is required."}
# It needs a dedicated RODIN_API_KEY issued from the API dashboard.
# Spec is from the official docs (docs.hyper3d.ai/en/api-specification/rodin-gen2-5),
# Confirmed via live 401 probes on /api/v2/{rodin,status,download,check_balance,bang}.
#
# STATUS: unverified end-to-end (Turnstile IP-rate-limited during the probe).
# Treat as documented-Hypothesis until a real API key is used.
THREED = {
    "transport": "POST https://api.hyper3d.com/api/v2/rodin",
    "content_type": "multipart/form-data",
    "auth": "Authorization: Bearer <RODIN_API_KEY>  (separate from the web JWT)",
    "response": {
        "201": {"message": "Submitted.", "uuid": "<task uuid>",
                "jobs": {"uuids": ["<job uuid>"], "subscription_key": "<key>"},
                "consumed": 0.5},
        "note": "use jobs.subscription_key for /api/v2/status, top-level uuid for /api/v2/download",
    },
    "lifecycle": {
        "submit": "POST /api/v2/rodin",
        "status": "POST /api/v2/status   (body: subscription_key)",
        "download": "POST /api/v2/download (body: top-level uuid)",
        "balance": "GET  /api/v2/check_balance",
        "bang": "POST /api/v2/bang",
    },
    # tiers are the 3D "models"
    "tiers": ["Gen-2.5-Extreme-Low", "Gen-2.5-Low", "Gen-2.5-Medium",
              "Gen-2.5-High", "Gen-2.5-Extreme-High"],
    "tier_default": "Regular (Gen-1/1.5)",
    "tier_notes": {
        "Gen-2.5-Extreme-High": "also returns an extra mesh up to 10M faces",
        "creative": "geometry_instruct_mode=creative works on Gen-2.5-Medium+",
    },
    "params": {
        "images": "array<file> 1..5  (required for Image-to-3D; omit for Text-to-3D)",
        "prompt": "string <=1024  (required for Text-to-3D; optional guidance for Image-to-3D)",
        "tier": "string REQUIRED (else falls back to Gen-1/1.5)",
        "mesh_mode": "Quad | Raw (Gen-2.5 default Raw)",
        "geometry_file_format": "glb | usdz | fbx | obj | stl  (default glb)",
        "texture_mode": "legacy | extreme-low | low | medium | high | extreme-high",
        "material": "PBR | Shaded | Hybrid | All | None  (default PBR)",
        "quality": "high | medium | low | extra-low (Quad default medium / Raw default medium)",
        "quality_override": "int 500..2000000",
        "seed": "int 0..65535",
        "bbox_condition": "array<int> exactly 3 (x,y,z)",
        "addons": "array<string> e.g. HighPack (2K->4K textures)",
        "quad_normal": "bool (bake hi-res into normal map)",
        "soft": "bool (smooth edges)",
        "detail_level": "int 1..3",
        "TAPose": "bool (human rest-pose)",
        "use_original_alpha": "bool",
        "preview_render": "bool (extra HQ render)",
        "is_symmetric": "symmetric | balanced | asymmetric | unknown",
        "image_label": "array<string> viewing direction per image",
    },
    "pricing": {
        "base": "0.5 credits",
        "Gen-2.5-Extreme-High": "+0.5",
        "texture_mode=extreme-high": "+2.0",
        "note": "additive",
    },
    "fast_mode": "tier!=Extreme-High && geometry_file_format=glb && mesh_mode=Raw && quality_override<=20000",
}


# ---------------------------------------------------------------------------
# Verified-usable on a Free account (2026-10-08 live tests)
# ---------------------------------------------------------------------------
USABLE = {
    "pixverse": {
        "provider": "pixverse",
        "family": "video",
        "api": "text_to_video",
        "label": "PixVerse (Default)",
        "models": ["v6", "v5.6", "v5.5", "v5", "c1"],
        "default_model": "v6",
        "defaults": {"aspect_ratio": "16:9", "resolution": "720p", "duration": 5, "audio": True},
        "aspect_ratios": ["16:9", "4:3", "3:4", "1:1", "9:16", "2:3", "3:2", "21:9"],
        "resolutions": ["360p", "540p", "720p", "1080p"],
        "durations": [5, 8, 10, 15],
        "verified": ["v6 -> 5.04s MP4 1,031,930B", "v5.6 -> 5.04s MP4 570,419B"],
    },
    "seedream-4.5": {
        "provider": "doubao",
        "family": "image",
        "api": "text_to_image",
        "label": "Seedream 4.5",
        "models": ["seedream_4_5"],
        "default_model": "seedream_4_5",
        "defaults": {"size": "2048x2048"},
        "sizes": ["1024x1024", "2048x2048", "2304x1728", "1728x2304", "3456x2592",
                  "2592x3456", "4096x4096", "1440x2560", "2560x1440"],
        "verified": ["2048x2048 JPEG 907,384B"],
    },
    "seedream-5.0-lite": {
        "provider": "doubao",
        "family": "image",
        "api": "text_to_image",
        "label": "Seedream 5.0 lite",
        "models": ["seedream_5_0_lite"],
        "default_model": "seedream_5_0_lite",
        "defaults": {"size": "2048x2048"},
        "sizes": ["1024x1024", "2048x2048", "2304x1728", "1728x2304", "3072x3072",
                  "3456x2592", "2592x3456", "1440x2560", "2560x1440"],
        "verified": ["2048x2048 JPEG 314,467B"],
    },
}

# Aliases so OpenAI-ish / pixverse-ish names resolve.
ALIASES = {
    "pixverse": "pixverse",
    "default": "pixverse",
    "pixverse-v6": "pixverse",
    "pixverse-v5.6": "pixverse",
    "pixverse-v5": "pixverse",
    "seedream-4.5": "seedream-4.5",
    "seedream_4_5": "seedream-4.5",
    "seedream-45": "seedream-4.5",
    "seedream-5.0-lite": "seedream-5.0-lite",
    "seedream_5_0_lite": "seedream-5.0-lite",
    "seedream-50-lite": "seedream-5.0-lite",
}

# ---------------------------------------------------------------------------
# Known-unavailable (kept so /v1/models can explain *why*, not silently omit)
# ---------------------------------------------------------------------------
BLOCKED = {
    "sora-2":        {"reason": "provider 'sora2' is not in the server provider enum (12 values) -> HTTP 400", "class": "invalid_provider"},
    "sora-2-pro":    {"reason": "provider 'sora2' is not in the server provider enum -> HTTP 400", "class": "invalid_provider"},
    "luma":          {"reason": "provider 'luma' is not in the server provider enum -> HTTP 400", "class": "invalid_provider"},
    "luma-pro":      {"reason": "provider 'luma' is not in the server provider enum -> HTTP 400", "class": "invalid_provider"},
    "minimax-h3-max": {"reason": "NEED_ADVANCED_SUBSCRIPTION (server-side paywall)", "class": "paywall"},
    "minimax-h3-max-turbo": {"reason": "NEED_ADVANCED_SUBSCRIPTION (server-side paywall)", "class": "paywall"},
    "veo3":          {"reason": "NEED_ADVANCED_SUBSCRIPTION (server-side paywall)", "class": "paywall"},
    "veo3-pro":      {"reason": "NEED_ADVANCED_SUBSCRIPTION (server-side paywall)", "class": "paywall"},
    "seedance-2.0":  {"reason": "bundle marks Free:false (paywalled)", "class": "paywall"},
    "seedance-2.0-fast": {"reason": "bundle marks Free:false (paywalled)", "class": "paywall"},
    "seedance-2.5":  {"reason": "bundle marks Free:false (paywalled)", "class": "paywall"},
    "gpt-image-2":   {"reason": "bundle marks Free:false (paywalled)", "class": "paywall"},
    "nano-banana-2": {"reason": "bundle marks Free:false (paywalled)", "class": "paywall"},
}

# Server-accepted provider enum (12 values) — for validation.
SERVER_PROVIDERS = {
    "pixverse", "other", "minimax", "flux2", "nano_banana", "veo", "doubao",
    "gpt", "rz_51", "rz_52", "doubao_or_local", "default",
}

# model whitelist per provider (discovered from server 400s)
PROVIDER_MODELS = {
    "pixverse": {"v5", "v5.5", "v5.6", "v6", "c1"},
    "minimax": {"h3_max", "h3_max_turbo"},
    "doubao": {"seedream_4_5", "seedream_5_0_lite", "seedance_2_0",
               "seedance_2_0_fast", "seedance_2_5"},
    "veo": {None},
}

API_ENUM = {
    "text_to_video", "text_to_video/pro", "image_to_video", "image_to_video/pro",
    "video_to_video", "transition", "reference", "any_to_video",
    "text_to_image", "text_to_image/pro", "image_to_image", "image_to_image/pro",
    "image_enhance",
}


def resolve(name):
    """Return (canonical_key, spec) or (None, None).

    Accepts both family keys ('pixverse', 'seedream-4.5') and bare upstream
    model ids ('v6', 'seedream_4_5') since /v1/models advertises the latter.
    """
    if not name:
        return None, None
    key = str(name).strip().lower()
    key = ALIASES.get(key, key)
    spec = USABLE.get(key)
    if spec:
        return key, spec
    # bare upstream model id -> family
    for fam, sp in USABLE.items():
        if key in [m.lower() for m in sp["models"]]:
            return fam, sp
    return None, None


def pick_model(name, spec):
    """If the caller named a specific upstream version, honour it."""
    if not name:
        return spec["default_model"]
    raw = str(name).strip()
    for m in spec["models"]:
        if raw == m:
            return m
    low = raw.lower()
    for m in spec["models"]:
        if low == m.lower():
            return m
    # strip a family prefix like "pixverse:"
    if ":" in raw:
        tail = raw.split(":", 1)[1]
        for m in spec["models"]:
            if tail.lower() == m.lower():
                return m
    return spec["default_model"]


def blocked_reason(name):
    return BLOCKED.get(str(name).strip().lower())


def list_3d_models():
    """3D tiers as models. NOTE: these go through /api/v2/rodin (API-key gated),
    NOT the /api/gateway/* path the other models use."""
    now = 1757289600
    return [
        {
            "id": f"rodin:{t}",
            "object": "model",
            "created": now,
            "owned_by": "hyper3d/rodin",
            "family": "3d",
            "label": t,
            "tier": t,
            "endpoint": "/api/v2/rodin",
            "auth": "RODIN_API_KEY",
            "verified": [],
            "status": "documented (needs API key; not live-verified)",
        }
        for t in THREED["tiers"]
    ]


def list_models():
    """OpenAI-compatible /v1/models payload."""
    data = list(list_3d_models())
    for key, spec in USABLE.items():
        for m in spec["models"]:
            if spec["family"] == "video":
                data.append({
                    "id": m if spec["provider"] == "pixverse" else f"{key}:{m}",
                    "object": "model",
                    "created": 1757289600,
                    "owned_by": f"hyper3d/{spec['provider']}",
                    "family": "video",
                    "label": spec["label"],
                    "verified": spec["verified"],
                })
            else:
                data.append({
                    "id": m,
                    "object": "model",
                    "created": 1757289600,
                    "owned_by": f"hyper3d/{spec['provider']}",
                    "family": "image",
                    "label": spec["label"],
                    "verified": spec["verified"],
                })
    return {"object": "list", "data": data}