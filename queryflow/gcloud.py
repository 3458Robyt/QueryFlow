"""Stable, credential-free execution context for Google Cloud CLI calls."""

from __future__ import annotations

import os
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class GcloudContext:
    """Pin gcloud subprocesses to one profile-owned configuration directory."""

    config_dir: Path
    account: str | None = None

    @classmethod
    def default(cls, *, account: str | None = None) -> "GcloudContext":
        return cls(Path.home() / ".config" / "gcloud", account=account)

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        environment = dict(base or os.environ)
        environment["CLOUDSDK_CONFIG"] = str(self.config_dir.expanduser())
        return environment

    def command(self, args: Sequence[str], *, account: str | None = None) -> list[str]:
        values = [item for item in args if not str(item).startswith("--account=")]
        if values and values[0] == "gcloud":
            values = values[1:]
        selected_account = account or self.account
        prefix = ["gcloud"]
        if selected_account:
            prefix.append(f"--account={selected_account}")
        return [*prefix, *values]

    def run(
        self,
        args: Sequence[str],
        *,
        account: str | None = None,
        check: bool = False,
        capture_output: bool = True,
        text: bool = True,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(args, account=account),
            check=check,
            capture_output=capture_output,
            text=text,
            env=self.environment(),
            **kwargs,
        )

    def json(self, args: Sequence[str], *, account: str | None = None) -> object:
        completed = self.run(args, account=account)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "gcloud falló")
        try:
            return json.loads(completed.stdout or "null")
        except json.JSONDecodeError as error:
            raise RuntimeError("gcloud no devolvió JSON válido") from error
