"""Install user timers for this checkout. Does not contain or copy credentials."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = ROOT / "deploy/systemd"
    destination = Path.home() / ".config/systemd/user"
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.glob("trendradar-content-*"):
        # Units are templates in the repo; support a different checkout location.
        text = path.read_text().replace("/home/chenyy/TrendRadar", str(ROOT))
        (destination / path.name).write_text(text)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        [
            "systemctl",
            "--user",
            "enable",
            "--now",
            "trendradar-content-collect.timer",
            "trendradar-content-daily.timer",
        ],
        check=True,
    )
    subprocess.run(
        ["systemctl", "--user", "list-timers", "trendradar-content-*", "--no-pager"],
        check=True,
    )


if __name__ == "__main__":
    main()
