"""Interactive prompt builder driven directly by Catalog field definitions.

This is the CLI equivalent of what would have been a JSON-Schema-driven web
form: for every catalog field with `source: user_input`, render an
appropriate `questionary` prompt from its `type`, rather than hand-coding a
form per install method. Adding a field to a catalog YAML file is enough to
make it show up in the wizard -- no code change needed.
"""

from __future__ import annotations

from typing import Any

import questionary

from clusterbuild.core.catalog_loader import CatalogEntry


def _prompt_scalar(field_def: dict[str, Any]) -> Any:
    label = field_def.get("label", field_def["path"])
    default = field_def.get("default")
    field_type = field_def.get("type", "string")

    if field_type == "int":
        raw = questionary.text(f"{label}:", default=str(default) if default is not None else "").ask()
        return int(raw)
    if field_type in ("ipv4", "string"):
        return questionary.text(f"{label}:", default=str(default) if default is not None else "").ask()
    if field_type == "secret":
        return questionary.password(f"{label}:").ask()
    if field_type.startswith("list["):
        raw = questionary.text(f"{label} (comma-separated):").ask()
        return [v.strip() for v in raw.split(",") if v.strip()]
    if field_type.startswith("enum["):
        options = field_type[len("enum["):-1].split(",")
        return questionary.select(f"{label}:", choices=options).ask()
    # Fallback: treat as free text.
    return questionary.text(f"{label}:").ask()


def _prompt_host_list(field_def: dict[str, Any]) -> list[dict[str, Any]]:
    hosts: list[dict[str, Any]] = []
    console_label = field_def.get("label", field_def["path"])
    questionary.print(f"{console_label} -- add hosts one at a time, leave hostname blank to finish.")
    item_schema = field_def.get("item_schema", [])
    while True:
        hostname = questionary.text("  hostname (blank to finish):").ask()
        if not hostname:
            break
        host: dict[str, Any] = {"hostname": hostname}
        for item in item_schema:
            if item["field"] == "hostname":
                continue
            key = item["field"]
            item_type = item.get("type", "string")
            default = item.get("default")
            if item_type.startswith("enum["):
                options = item_type[len("enum["):-1].split(",")
                host[key] = questionary.select(f"  {key}:", choices=options).ask()
            elif item_type == "int":
                raw = questionary.text(f"  {key}:", default=str(default) if default is not None else "").ask()
                host[key] = int(raw)
            else:
                host[key] = questionary.text(f"  {key}:", default=str(default) if default is not None else "").ask()
        hosts.append(host)
    return hosts


def collect_answers(entry: CatalogEntry, *, skip_paths: set[str] | None = None) -> dict[str, Any]:
    """Walk every `user_input` field across all of `entry.manifests` and prompt for it."""
    skip_paths = skip_paths or set()
    answers: dict[str, Any] = {}
    for manifest in entry.manifests:
        for field_def in manifest.get("fields", []):
            if field_def.get("source") != "user_input":
                continue
            path = field_def["path"]
            if path in skip_paths:
                continue
            field_type = field_def.get("type", "string")
            if field_type == "list[host]":
                answers[path] = _prompt_host_list(field_def)
            else:
                answers[path] = _prompt_scalar(field_def)
    return answers
