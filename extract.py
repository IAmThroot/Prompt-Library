"""Read generation metadata from ComfyUI outputs and pull the text prompts out of it.

Pure functions only (no ComfyUI imports), so everything here is easy to test.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess

EXTRACTOR_VERSION = 3

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
AUDIO_EXT = {".flac", ".mp3", ".wav", ".ogg", ".opus", ".m4a"}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".avi"}

# Inputs that hold prompt text on text-encoder style nodes.
TEXT_KEYS = ("text", "text_g", "text_l", "clip_l", "clip_g", "t5xxl", "prompt", "tags", "lyrics")
# Inputs worth following when a text input is wired to a string/primitive node.
STR_KEYS = ("text", "string", "value", "prompt", "text_a", "positive_prompt", "wildcard_text", "populated_text")
# Links we never follow when walking up the conditioning chain.
SKIP_KEYS = {"model", "vae", "clip", "clip_vision", "latent_image", "latent", "samples",
             "image", "images", "mask", "control_net", "controlnet"}


def media_type(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return None


# --------------------------------------------------------------------------- #
# Reading raw metadata out of files
# --------------------------------------------------------------------------- #

def read_media(path, mtype):
    """Return metadata, actual media kind and a diagnostic; never hide reader failures."""
    try:
        if os.path.splitext(path)[1].lower() == ".avif":
            try:
                from PIL import Image
                with Image.open(path) as image:
                    animated = getattr(image, "is_animated", False)
                return _read_image(path), "video" if animated else "image", None
            except Exception:
                meta, streams = _probe(path)
                animated = any(int(s.get("nb_frames", 0) or 0) > 1 for s in streams)
                return meta, "video" if animated else "image", None
        return (_read_image(path) if mtype == "image" else _read_av(path)), mtype, None
    except Exception as exc:
        return {}, mtype, f"{type(exc).__name__}: {exc}"


def read_metadata(path, mtype):
    return read_media(path, mtype)[0]


def _probe(path):
    if not shutil.which("ffprobe"):
        raise RuntimeError("Install PyAV for audio/video, or FFmpeg (ffprobe) as a fallback")
    result = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                             "-of", "json", os.path.abspath(path)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:1000] or "ffprobe could not read this file")
    data = json.loads(result.stdout)
    out = {k.lower(): v for k, v in data.get("format", {}).get("tags", {}).items()}
    for stream in data.get("streams", []):
        for key, value in stream.get("tags", {}).items():
            out.setdefault(key.lower(), value)
    return out, data.get("streams", [])


def _read_image(path):
    from PIL import Image

    out = {}
    with Image.open(path) as img:
        for key, val in (img.info or {}).items():
            if isinstance(val, bytes) and key == "comment":
                val = val.decode("utf-8", "ignore")
            if isinstance(val, str):
                out[str(key)] = val
        try:
            exif = img.getexif()
        except Exception:
            exif = None
        # ComfyUI stores "prompt:{json}" / "workflow:{json}" in EXIF tags for webp/jpeg.
        for val in list(exif.values()) if exif else []:
            if isinstance(val, bytes):
                val = val.decode("utf-8", "ignore")
            if not isinstance(val, str):
                continue
            val = val.rstrip("\x00")
            for key in ("prompt", "workflow"):
                if val.startswith(key + ":"):
                    out.setdefault(key, val[len(key) + 1:])
    return out


def _read_av(path):
    try:
        import av
    except ImportError:
        return _probe(path)[0]

    out = {}
    with av.open(path) as container:
        for key, val in (container.metadata or {}).items():
            out[str(key).lower()] = val
        for stream in container.streams:
            for key, val in (stream.metadata or {}).items():
                out.setdefault(str(key).lower(), val)
    return out


# --------------------------------------------------------------------------- #
# Turning metadata into prompts
# --------------------------------------------------------------------------- #

def _loads(s):
    if not isinstance(s, str):
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _is_graph(g):
    return isinstance(g, dict) and any(isinstance(v, dict) and "class_type" in v for v in g.values())


def _is_link(v):
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], (str, int)) and isinstance(v[1], int)


def _text(value):
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return "\n".join(value)
    return value if isinstance(value, str) else ""


def plan_prompts(plan):
    """Read ordered H3 scenes, accepting both string and line-array serialization."""
    plan = _loads(plan) if isinstance(plan, str) else plan
    if not isinstance(plan, dict):
        return []
    if isinstance(plan.get("editor_plan"), dict):
        plan = plan["editor_plan"]
    prefix = norm(_text(plan.get("prompt_prefix")))
    found = []
    for shot in plan.get("shots", plan.get("segments", [])):
        if not isinstance(shot, dict) or shot.get("enabled") is False:
            continue
        prompt = norm(_text(shot.get("prompt")))
        if prompt:
            found.append("\n".join(x for x in (prefix, prompt) if x))
    return found


def _plain_result(positive, negative="", source="metadata"):
    positive, negative = norm(positive), norm(negative)
    if not (positive or negative):
        return None
    return {"positive": positive, "negative": negative, "extra": [], "meta": {},
            "provenance": {"source": source}, "warnings": [], "stages": []}


def extract_from_metadata(meta, output_hint=None):
    meta = {str(k).lower(): v for k, v in meta.items()}
    # Final assembled scene metadata is more specific than the editor's API graph.
    for key in ("h3_manifest", "h3_plan"):
        scenes = plan_prompts(meta.get(key))
        if scenes:
            result = _plain_result(scenes[0], source=key)
            result["extra"] = [{"role": "positive", "text": t, "label": f"Scene {i}"}
                               for i, t in enumerate(scenes[1:], 2)]
            return result
    graph = _loads(meta.get("prompt"))
    if _is_graph(graph):
        result = extract_prompts(graph, output_hint)
        if result:
            return result
    for key in ("comment", "description", "usercomment"):
        data = _loads(meta.get(key))
        if not isinstance(data, dict):
            continue
        inner = data.get("prompt")
        parsed = _loads(inner) if isinstance(inner, str) else inner
        candidate = parsed if _is_graph(parsed) else data if _is_graph(data) else None
        if candidate:
            result = extract_prompts(candidate, output_hint)
            if result:
                return result
        elif isinstance(inner, str) and inner.strip() and parsed is None:
            result = _plain_result(inner, _text(data.get("negative_prompt")), key)
            if result:
                return result
    workflow = _loads(meta.get("workflow"))
    if isinstance(workflow, dict):
        converted = workflow_graph(workflow)
        if converted:
            result = extract_prompts(converted, output_hint)
            if result:
                result["warnings"].append("Recovered from editor workflow; runtime text may differ")
                return result
    if isinstance(meta.get("parameters"), str):
        return parse_a1111(meta["parameters"])
    return None


def workflow_graph(workflow):
    """Conservative fallback: only known widget layouts, never guess widget positions."""
    layouts = {"CLIPTextEncode": ("text",), "PromptLibrary": ("positive", "negative"),
               "PrimitiveStringMultiline": ("value",), "Text Multiline": ("text",)}
    links = {str(x[0]): [str(x[1]), x[2]] for x in workflow.get("links", [])
             if isinstance(x, list) and len(x) >= 5}
    graph = {}
    for node in workflow.get("nodes", []):
        if not isinstance(node, dict) or node.get("mode") in (2, 4):
            continue
        cls = node.get("type", "")
        ins = {}
        values = node.get("widgets_values", [])
        if isinstance(values, list):
            ins.update(zip(layouts.get(cls, ()), values))
        for entry in node.get("inputs", []):
            if str(entry.get("link")) in links:
                ins[entry["name"]] = links[str(entry["link"]) ]
        graph[str(node.get("id"))] = {"class_type": cls, "inputs": ins,
                                      "_meta": {"title": node.get("title", "")}}
    return graph


def parse_a1111(text):
    """Automatic1111-style 'parameters' text chunk."""
    text = text.replace("\r\n", "\n")
    head, _, params = text.partition("\nSteps:")
    pos, _, neg = head.partition("\nNegative prompt:")
    pos, neg = norm(pos), norm(neg)
    pos = "" if pos == "positive_prompt_not_found" else pos
    neg = "" if neg == "negative_prompt_not_found" else neg
    if not (pos or neg):
        return None
    full = "Steps:" + params if params else ""
    meta = {}
    for key, pat in (("steps", r"Steps: (\d+)"), ("sampler", r"Sampler: ([^,]+)"),
                     ("cfg", r"CFG scale: ([\d.]+)"), ("seed", r"Seed: (\d+)"),
                     ("size", r"Size: (\d+x\d+)"), ("model", r"Model: ([^,]+)")):
        m = re.search(pat, full)
        if m:
            meta[key] = m.group(1).strip()
    return {"positive": pos, "negative": neg, "extra": [], "meta": meta}


def norm(text):
    return (text if isinstance(text, str) else "").replace("\r\n", "\n").strip()


def prompt_hash(positive, negative, extra=None):
    blob = json.dumps([norm(positive), norm(negative)], ensure_ascii=False)
    if extra:
        blob += json.dumps([(e.get("role"), norm(e.get("text"))) for e in extra], ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def resolve_text(nodes, value, depth=0):
    """A text input is either a literal string or a link to a string/primitive node."""
    if isinstance(value, str):
        return value
    if not _is_link(value) or depth > 8:
        return None
    node = nodes.get(str(value[0]))
    if not node:
        return None
    ins = node.get("inputs") or {}
    if node.get("class_type") == "PromptLibrary":
        return resolve_text(nodes, ins.get("negative" if value[1] == 1 else "positive"), depth + 1)
    cls = node.get("class_type", "")
    if cls == "StringConcatenate" and value[1] == 0:
        parts = [resolve_text(nodes, ins.get(k), depth + 1) for k in ("string_a", "string_b")]
        if any(p is None for p in parts):
            return None
        delim = ins.get("delimiter", "")
        return delim.join(parts) if isinstance(delim, str) else None
    if cls == "ComfySwitchNode" and value[1] == 0 and isinstance(ins.get("switch"), bool):
        return resolve_text(nodes, ins.get("on_true" if ins["switch"] else "on_false"), depth + 1)
    string_nodes = {"PrimitiveString": "value", "PrimitiveStringMultiline": "value",
                    "Text Multiline": "text", "String": "string"}
    if cls in string_nodes and value[1] == 0:
        return resolve_text(nodes, ins.get(string_nodes[cls]), depth + 1)
    return None


def _nid_key(nid):
    return (0, int(nid), "") if nid.isdigit() else (1, 0, nid)


def _uniq(items):
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


PROMPT_FIELDS = {
    "MiniMaxH3ImageToVideo": ("prompt",), "MiniMaxH3ReferenceToVideo": ("prompt",),
    "MiniMaxH3Director": ("prompt",), "Krea2EditGroundedEncode": ("prompt",),
    "Prompt (LoraManager)": ("text",), "Flux2KleinSectionedEncoder": ("combined_prompt",),
    "PainterFluxImageEdit": ("prompt",), "H3TextToImagePrepare": ("prompt",),
}


def _is_encoder(node):
    cls = str(node.get("class_type", ""))
    return cls in PROMPT_FIELDS or "textencode" in cls.lower()


def extract_prompts(graph, output_hint=None):
    """Extract paired generation stages and their provenance; auxiliary text stays separate."""
    nodes = {str(k): v for k, v in graph.items() if isinstance(v, dict) and v.get("class_type")}
    warnings = []
    def inputs(nid):
        value = nodes[nid].get("inputs", {})
        return value if isinstance(value, dict) else {}
    def dependencies(nid):
        ins = inputs(nid)
        if nodes[nid]["class_type"] == "ComfySwitchNode":
            if isinstance(ins.get("switch"), bool):
                value = ins.get("on_true" if ins["switch"] else "on_false")
                return [str(value[0])] if _is_link(value) else []
            warnings.append(f"Runtime switch #{nid}: both candidate branches retained")
        return [str(v[0]) for v in ins.values() if _is_link(v)]
    def ancestors(starts):
        seen, pending = set(), list(starts)
        while pending:
            nid = pending.pop()
            if nid in seen or nid not in nodes:
                continue
            seen.add(nid)
            pending.extend(dependencies(nid))
        return seen
    def branch_nodes(start):
        ordered, pending = {}, [start]
        while pending:
            nid = pending.pop(0)
            if nid in ordered or nid not in nodes:
                continue
            ordered[nid] = nodes[nid]
            pending.extend(dependencies(nid))
        return ordered
    def encoder_text(nid):
        cls = nodes[nid]["class_type"]
        if cls == "Prompt (LoraManager)" and any(k.startswith("trigger_words") and _is_link(v) for k, v in inputs(nid).items()):
            warnings.append(f"LoRA trigger additions at #{nid} are runtime values; saved text is the authored prompt")
        if cls == "Flux2KleinSectionedEncoder" and not norm(inputs(nid).get("combined_prompt")):
            parts = [norm(resolve_text(nodes, inputs(nid).get(k))) for k in ("front_text", "mid_text", "end_text")]
            parts = [p for p in parts if p]
            if parts:
                warnings.append(f"Sectioned prompt #{nid} recovered from sections; verify separator formatting")
                separator = {"comma": ", ", "newline": "\n", "space": " "}.get(inputs(nid).get("separator"), "\n\n")
                return separator.join(parts)
        keys = PROMPT_FIELDS.get(cls, TEXT_KEYS)
        texts = []
        for key in keys:
            if key not in inputs(nid):
                continue
            value = resolve_text(nodes, inputs(nid)[key])
            if value is None and _is_link(inputs(nid)[key]):
                warnings.append(f"Unresolved text: {cls} #{nid}.{key}")
            if norm(value):
                texts.append(norm(value))
        return "\n\n".join(_uniq(texts))
    def collect(link, seen=None):
        if not _is_link(link):
            return []
        seen = set() if seen is None else seen
        nid, slot = str(link[0]), link[1]
        if nid not in nodes or (nid, slot) in seen:
            return []
        seen.add((nid, slot))
        cls = nodes[nid]["class_type"]
        if cls == "ConditioningZeroOut":
            warnings.append(f"Conditioning is zeroed at #{nid}; source text is not a negative prompt")
            return []
        if _is_encoder(nodes[nid]):
            text = encoder_text(nid)
            return [(nid, text)] if text else []
        ins = inputs(nid)
        if cls == "ComfySwitchNode":
            selector = ins.get("switch")
            if isinstance(selector, bool):
                return collect(ins.get("on_true" if selector else "on_false"), seen)
            warnings.append(f"Runtime conditioning switch #{nid}: both candidates retained")
        keys = [k for k in ins if k != "switch"]
        if _is_link(ins.get("positive")) and _is_link(ins.get("negative")):
            if slot not in (0, 1):
                warnings.append(f"Unknown conditioning output: {cls} #{nid}:{slot}")
                return []
            keys = ["positive" if slot == 0 else "negative"]
        # Follow conditioning/guide connections, not unrelated image/model inputs.
        result = []
        for key in keys:
            if key not in SKIP_KEYS and _is_link(ins.get(key)):
                result.extend(collect(ins[key], seen))
        return result

    sinks = [nid for nid in nodes if any(x in nodes[nid]["class_type"].lower()
             for x in ("saveimage", "savevideo", "videosave", "videocombine", "saveaudio", "metadatimagesaver", "metadataimagesaver", "chainassemble", "chainsegmentsave"))]
    selected = sinks
    if output_hint and sinks:
        basename = os.path.splitext(os.path.basename(output_hint))[0]
        matches = [nid for nid in sinks if isinstance(inputs(nid).get("filename_prefix"), str)
                   and inputs(nid)["filename_prefix"]
                   and basename.startswith(os.path.basename(inputs(nid)["filename_prefix"]))
                   and "%" not in inputs(nid)["filename_prefix"]]
        if len(matches) == 1:
            selected = matches
    if len(selected) > 1:
        warnings.append("Multiple output branches; all candidate generation stages are retained")
    active_order = {}
    for sink in selected:
        active_order.update(branch_nodes(sink))
    if not selected:
        active_order = dict(nodes)
    active = set(active_order)
    rank = {nid: index for index, nid in enumerate(active_order)}
    consumers = [nid for nid in sorted(active, key=lambda i: ("sampler" not in nodes[i]["class_type"].lower(), rank[i]))
                 if "sampler" in nodes[nid]["class_type"].lower() or "guider" in nodes[nid]["class_type"].lower()]
    def guide_pair(link, visited=None):
        if not _is_link(link):
            return [], []
        visited = set() if visited is None else visited
        nid = str(link[0])
        if nid not in nodes or nid in visited:
            return [], []
        visited.add(nid)
        ins = inputs(nid)
        if "positive" in ins or "negative" in ins:
            return collect(ins.get("positive")), collect(ins.get("negative"))
        if "conditioning" in ins:
            return collect(ins["conditioning"]), []
        for key, value in ins.items():
            if key not in SKIP_KEYS and _is_link(value):
                p, n = guide_pair(value, visited)
                if p or n:
                    return p, n
        return [], []
    stages, used = [], set()
    for nid in consumers:
        ins = inputs(nid)
        p = collect(ins.get("positive")) or collect(ins.get("conditioning"))
        n = collect(ins.get("negative"))
        if _is_link(ins.get("guider")):
            p, n = guide_pair(ins["guider"])
        if not (p or n):
            continue
        used.update(i for i, t in p + n)
        positive = "\n\n".join(_uniq([t for i, t in p]))
        negative = "\n\n".join(_uniq([t for i, t in n]))
        if "guider" in nodes[nid]["class_type"].lower() and any(
                s["positive"] == positive and s["negative"] == negative and nid in ancestors([s["node"]])
                for s in stages):
            continue
        stages.append({"node": nid, "positive": positive, "negative": negative,
                       "meta": extract_meta(branch_nodes(nid)),
                       "sources": [i for i, t in p + n]})
    # H3 loop outputs are runtime values; recover the ordered plan rather than guessing a string slot.
    plans = []
    for nid in sorted(active, key=_nid_key):
        if nodes[nid]["class_type"] in ("MiniMaxH3ChainPlanStudio", "MiniMaxH3ChainPlanModern"):
            scenes = plan_prompts(inputs(nid).get("plan_json"))
            if scenes:
                plans.append((nid, scenes))
    if not stages and plans:
        nid, scenes = plans[-1]
        stages = [{"node": nid, "positive": t, "negative": "", "meta": {}, "sources": [nid]}
                  for t in scenes]
        warnings = [w for w in warnings if not w.startswith("Unresolved text:")]
    candidates = []
    for nid in sorted(active, key=_nid_key):
        if _is_encoder(nodes[nid]) and nid not in used:
            text = encoder_text(nid)
            if text:
                candidates.append((nid, text))
    if not stages:
        # Never promote text used by segmentation into the generation prompt.
        auxiliary = set()
        for nid in nodes:
            if "sam3" in nodes[nid]["class_type"].lower():
                auxiliary.update(ancestors([nid]))
        for nid, text in candidates:
            if nid in auxiliary:
                continue
            title = str((nodes[nid].get("_meta") or {}).get("title", "")).lower()
            stages.append({"node": nid, "positive": "" if "negative" in title else text,
                           "negative": text if "negative" in title else "", "meta": {}, "sources": [nid]})
        for nid in sorted(active, key=_nid_key):
            if nodes[nid]["class_type"] == "PromptLibrary":
                p = norm(resolve_text(nodes, inputs(nid).get("positive")))
                n = norm(resolve_text(nodes, inputs(nid).get("negative")))
                if p or n:
                    stages.append({"node": nid, "positive": p, "negative": n, "meta": {}, "sources": [nid]})
        if len(stages) == 2 and sum(bool(s["positive"]) for s in stages) == 1 and sum(bool(s["negative"]) for s in stages) == 1:
            stages = [{"node": stages[0]["node"], "positive": next(s["positive"] for s in stages if s["positive"]),
                       "negative": next(s["negative"] for s in stages if s["negative"]), "meta": {},
                       "sources": [i for s in stages for i in s["sources"]]}]
        if stages:
            warnings.append("No resolved sampler branch; review prompt candidates")
    if not stages:
        return None
    main = stages[0]
    if len(stages) > 1:
        warnings.append("Multiple generation stages retained; the first is shown as the main prompt")
    extra = []
    for index, stage in enumerate(stages[1:], 2):
        for role in ("positive", "negative"):
            if stage[role]:
                extra.append({"role": role, "text": stage[role], "label": f"Stage {index} {role}"})
    stage_sources = {i for s in stages for i in s["sources"]}
    extra.extend({"role": "other", "text": t, "label": f"Auxiliary #{i}"}
                 for i, t in candidates if i not in stage_sources)
    return {"positive": main["positive"], "negative": main["negative"], "extra": extra,
            "meta": main["meta"], "stages": stages, "warnings": _uniq(warnings),
            "provenance": {"source": "api_graph", "outputs": selected}}


def _scalar(v):
    return v if isinstance(v, (int, float, str)) and not isinstance(v, bool) else None


def extract_meta(nodes):
    """Walk order is consumer-first so settings come from the nearest generation stage."""
    meta, loras = {}, []
    wanted = (("seed", "seed"), ("noise_seed", "seed"), ("steps", "steps"), ("cfg", "cfg"),
              ("sampler_name", "sampler"), ("scheduler", "scheduler"), ("denoise", "denoise"),
              ("ckpt_name", "model"), ("unet_name", "model"))
    for nid in nodes:
        ins = nodes[nid].get("inputs") or {}
        for key, name in wanted:
            val = _scalar(ins.get(key))
            if val is not None and name not in meta:
                meta[name] = str(val) if name == "seed" else val
        lora = _scalar(ins.get("lora_name"))
        if lora and lora != "None" and lora not in loras:
            loras.append(lora)
        w, h = _scalar(ins.get("width")), _scalar(ins.get("height"))
        if w and h and "latent" in str(nodes[nid]["class_type"]).lower() and "size" not in meta:
            meta["size"] = f"{w}x{h}"
    if loras:
        meta["loras"] = loras
    return meta
