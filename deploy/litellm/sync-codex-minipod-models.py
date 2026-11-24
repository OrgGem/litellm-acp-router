"""Register and update Mini-pod model aliases without exposing gateway credentials."""

import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "litellm"
CATALOG = ROOT / "deploy" / "codex-minipod" / "models.json"
BRIDGE_KEY = ROOT / ".local" / "minipod-codex" / "bridge" / "bridge-key"
MODEL_INFO = {
    "mode": "chat",
    "supports_function_calling": True,
    "supports_parallel_function_calling": True,
    "supports_tool_choice": True,
    "supports_native_streaming": True,
}


def read_env(path):
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def main():
    env = read_env(DEPLOY / ".env")
    base = "http://127.0.0.1:" + env.get("LITELLM_PORT", "14000")
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or catalog.get("codex-minipod") != "gpt-6-luna":
        raise RuntimeError("Unexpected Mini-pod model catalog")
    if not all(
        isinstance(alias, str) and isinstance(model, str)
        for alias, model in catalog.items()
    ):
        raise RuntimeError("Invalid Mini-pod model catalog")

    def api(path, body=None, *, method=None):
        request = Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + env["LITELLM_MASTER_KEY"],
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"LiteLLM returned HTTP {exc.code} for {path}") from None
        except URLError:
            raise RuntimeError(
                f"Could not contact LiteLLM for {path}; inspect current state before retrying"
            ) from None

    current = api("/model/info")["data"]
    existing = {}
    for item in current:
        existing.setdefault(item["model_name"], []).append(item)
    reference = existing.get("codex-minipod", [])
    if len(reference) != 1:
        raise RuntimeError("Expected one existing codex-minipod deployment")

    params = reference[0]["litellm_params"]
    if (
        params.get("model") != "openai/codex-minipod"
        or params.get("api_base") != "http://host.docker.internal:14002/v1"
    ):
        raise RuntimeError(
            "The existing codex-minipod deployment points somewhere unexpected"
        )
    if (
        params.get("use_chat_completions_api") is not True
        or params.get("caching") is not False
        or params.get("cache") != {"no-cache": True, "no-store": True}
        or params.get("max_retries") != 0
        or params.get("num_retries") != 0
    ):
        raise RuntimeError(
            "The existing deployment no longer has the expected Responses, cache, or retry settings"
        )

    def ensure_model_info(entry, alias):
        info = entry.get("model_info") or {}
        model_id = info.get("id")
        if model_id and any(info.get(k) != v for k, v in MODEL_INFO.items()):
            merged = {
                k: v
                for k, v in info.items()
                if k not in {"id", "db_model"} and v is not None
            }
            merged.update(MODEL_INFO)
            api(f"/model/{model_id}/update", {"model_info": merged}, method="PATCH")
            print(f"Updated capability metadata for {alias}")

    ensure_model_info(reference[0], "codex-minipod")

    aliases = [alias for alias in catalog if alias != "codex-minipod"]
    minipod_existing = {}
    for alias in aliases:
        entries = [
            entry
            for entry in existing.get(alias, [])
            if entry.get("litellm_params", {}).get("api_base") == params["api_base"]
        ]
        if len(entries) > 1:
            raise RuntimeError(f"Multiple Mini-pod deployments use alias {alias}")
        if entries:
            item = entries[0]["litellm_params"]
            if item.get("model") != "openai/" + alias:
                raise RuntimeError(
                    f"Existing alias {alias} points to another model name"
                )
            for name in (
                "use_chat_completions_api",
                "caching",
                "cache",
                "max_retries",
                "num_retries",
                "timeout",
            ):
                if item.get(name) != params.get(name):
                    raise RuntimeError(
                        f"Existing alias {alias} has a different {name} setting"
                    )
            minipod_existing[alias] = entries[0]

    bridge_key = BRIDGE_KEY.read_text(encoding="utf-8").strip()
    if not bridge_key:
        raise RuntimeError("Mini-pod bridge key is empty")
    for alias in aliases:
        if alias in minipod_existing:
            ensure_model_info(minipod_existing[alias], alias)
            print(f"Retained existing alias {alias}")
            continue
        new_params = {
            "model": "openai/" + alias,
            "api_base": params["api_base"],
            "api_key": bridge_key,
            "timeout": params.get("timeout", 110),
            "max_retries": params.get("max_retries", 0),
            "num_retries": params.get("num_retries", 0),
            "cache": params.get("cache", {"no-cache": True, "no-store": True}),
            "caching": False,
            "use_chat_completions_api": True,
        }
        api(
            "/model/new",
            {
                "model_name": alias,
                "litellm_params": new_params,
                "model_info": dict(MODEL_INFO),
            },
        )
        print(f"Registered alias {alias}")


if __name__ == "__main__":
    main()
