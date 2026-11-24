"""Register and update Claude Code and Antigravity bridge aliases in LiteLLM PostgreSQL."""

import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "litellm"
BRIDGES = (
    {
        "name": "claude-rpc",
        "catalog": ROOT / "deploy" / "claude-rpc" / "models.json",
        "key_file": ROOT / ".local" / "claude-rpc" / "bridge" / "bridge-key",
        "api_base": "http://host.docker.internal:14003/v1",
    },
    {
        "name": "antigravity-rpc",
        "catalog": ROOT / "deploy" / "antigravity-rpc" / "models.json",
        "key_file": ROOT / ".local" / "antigravity-rpc" / "bridge" / "bridge-key",
        "api_base": "http://host.docker.internal:14004/v1",
    },
)
MODEL_INFO = {
    "mode": "chat",
    "supports_function_calling": True,
    "supports_parallel_function_calling": True,
    "supports_tool_choice": True,
    "supports_native_streaming": True,
}


def read_env(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def main() -> None:
    env = read_env(DEPLOY / ".env")
    base = "http://127.0.0.1:" + env.get("LITELLM_PORT", "14000")

    def api(path: str, body=None, *, method=None):
        req = Request(
            base + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Authorization": "Bearer " + env["LITELLM_MASTER_KEY"],
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except HTTPError as exc:
            raise RuntimeError(f"LiteLLM returned HTTP {exc.code} for {path}") from None
        except URLError:
            raise RuntimeError(
                f"Could not contact LiteLLM for {path}; verify Docker stack is running"
            ) from None

    current = api("/model/info")["data"]
    existing: dict[str, list[dict]] = {}
    for item in current:
        existing.setdefault(item["model_name"], []).append(item)

    for bridge in BRIDGES:
        catalog = json.loads(bridge["catalog"].read_text(encoding="utf-8"))
        if not isinstance(catalog, dict) or not catalog:
            raise RuntimeError(f"Invalid catalog for {bridge['name']}")
        bridge_key = bridge["key_file"].read_text(encoding="utf-8").strip()
        if not bridge_key:
            raise RuntimeError(f"Empty bridge key for {bridge['name']}")
        api_base = bridge["api_base"]

        for alias in catalog:
            same_base = [
                e
                for e in existing.get(alias, [])
                if e.get("litellm_params", {}).get("api_base") == api_base
            ]
            other_base = [
                e
                for e in existing.get(alias, [])
                if e.get("litellm_params", {}).get("api_base") != api_base
            ]
            if other_base and not same_base:
                print(
                    f"Skipping {alias} ({bridge['name']}): already deployed elsewhere"
                )
                continue
            if len(same_base) > 1:
                raise RuntimeError(f"Multiple deployments found for {alias} on {api_base}")

            desired_params = {
                "model": "openai/" + alias,
                "api_base": api_base,
                "api_key": bridge_key,
                "timeout": 110,
                "max_retries": 0,
                "num_retries": 0,
                "cache": {"no-cache": True, "no-store": True},
                "caching": False,
                "use_chat_completions_api": True,
            }

            if same_base:
                entry = same_base[0]
                info = entry.get("model_info") or {}
                model_id = info.get("id")
                merged_info = {
                    k: v
                    for k, v in info.items()
                    if k not in {"id", "db_model"} and v is not None
                }
                merged_info.update(MODEL_INFO)
                if model_id:
                    api(
                        f"/model/{model_id}/update",
                        {
                            "litellm_params": desired_params,
                            "model_info": merged_info,
                        },
                        method="PATCH",
                    )
                    print(f"Updated existing alias {alias} ({bridge['name']})")
                continue

            api(
                "/model/new",
                {
                    "model_name": alias,
                    "litellm_params": desired_params,
                    "model_info": dict(MODEL_INFO),
                },
            )
            print(f"Registered alias {alias} ({bridge['name']})")


if __name__ == "__main__":
    main()
