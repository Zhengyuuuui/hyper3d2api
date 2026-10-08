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


def list_models():
    """OpenAI-compatible /v1/models payload."""
    data = []
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