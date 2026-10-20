# Running on a VPS

How to run WorldPrompt Core next to a dedicated Fabric server on a Linux VPS, headless, with no desktop. You need shell access, permission to run long-lived processes, and a Minecraft 26.3 server with Fabric (Java 25).

```text
Your browser --SSH tunnel--> Core settings   127.0.0.1:8765
Players ----------------------> Minecraft    :25565 (public)
                                  |
                                  +--> Core  127.0.0.1:8768 --> Codex CLI or Claude CLI --> model
```

Core and the mod both bind loopback only, so they must share a host and network namespace (two containers do not, unless they share one). Planning uses the subscription of the CLI account you sign in under; see [planner.md](planner.md#providers). The security model is in [architecture.md](architecture.md#security-model), and the wire details in [protocol.md](protocol.md).

## 1. Use one Linux user

Run Minecraft, Core and the provider CLI as the same user (`minecraft` below). Then the mod finds Core through its `connection.json` under that user's home, Core reads the CLI's saved sign-in, and the adapter token stays private to one account. For separate users, see [token file modes](#token-file-modes).

## 2. Install Core

Core needs Python 3.11 or newer (Ubuntu 22.04 ships 3.10; install `python3.12` or use the archive).

**From source or a wheel** (matches the supplied systemd unit):

```bash
git clone https://github.com/Ni-Cobra/worldprompt ~/worldprompt
cd ~/worldprompt
python3 -m venv .venv          # on Debian/Ubuntu: apt install python3-venv
.venv/bin/python -m pip install .
.venv/bin/worldprompt-core --version
```

The release wheel installs the same way; create `~/worldprompt` first if you use it, because the unit runs from there.

**From the Linux archive** (`WorldPrompt-Core-<version>-linux-x86_64.tar.gz` on the releases page): extract it and run `WorldPrompt-Core/WorldPrompt-Core serve --no-browser`. The CI build targets glibc 2.35, so it runs on Ubuntu 22.04 and newer. Point `ExecStart` below at that executable.

Install the [Codex CLI](https://developers.openai.com/codex/cli) or [Claude Code](https://code.claude.com/docs/en/setup) for the same user. A native executable avoids shell startup dependencies. If it is not on the service `PATH`, enter its absolute path in the Core settings ([command discovery](planner.md#command-discovery)).

## 3. Install the server and mod

Install Fabric Loader and Fabric API for Minecraft 26.3, then put the `worldprompt-fabric-<version>.jar` that matches your Core in `mods/` (install steps and compatibility are in [`fabric/README.md`](../fabric/README.md)). The mod creates `config/worldprompt.json` on first launch and needs no edits for this layout.

## 4. Sign in to the CLI over SSH

Sign in on the VPS, as the service user, not only on your own PC.

```bash
codex login --device-auth     # Codex: prints a link and code to enter in any browser
claude auth login             # Claude: prints a link; follow its prompts
```

Use a subscription sign-in: Core does not use API keys (it strips them from the CLI's environment, and rejects a Claude CLI signed in with an API key). If you prefer Codex's normal browser flow, forward its callback port while you log in: `ssh -L 1455:127.0.0.1:1455 minecraft@YOUR_VPS`. Core never reads or relays the CLI's credentials; when a session expires, sign in again the same way.

## 5. Start Core and open the settings

```bash
~/worldprompt/.venv/bin/worldprompt-core serve --no-browser
```

Core prints a private settings URL and the Minecraft service address:

```text
WorldPrompt Core <version>: http://127.0.0.1:8765/#session=<secret>
Minecraft service: http://127.0.0.1:8768
```

From your PC, tunnel the settings port and open that URL, fragment included, in your browser:

```bash
ssh -N -L 8765:127.0.0.1:8765 minecraft@YOUR_VPS
```

Choose the provider and model and check that the account reads as signed in. The secret changes on every Core start and grants full settings access, so do not share it. If you lose it, read the current one on the VPS from `~/.config/worldprompt/config.json.gui-access.json` (`session` and `port`) and build `http://127.0.0.1:<port>/#session=<session>`.

Core checks the Host header against its own port, so the tunnel must use the same port number on both ends. If 8765 is taken on your PC, set Core's `port` setting (or pass `--port 8785` once) and tunnel `8785:127.0.0.1:8785`.

Stop the foreground process with Ctrl+C before enabling the service.

## 6. Keep Core running

### User service

`packaging/worldprompt-core.service` is a systemd user unit for the layout above (`%h/worldprompt`, `.venv`). It runs `serve --no-browser --no-tray`, restarts on failure, and uses `UMask=0077`.

```bash
mkdir -p ~/.config/systemd/user
cp ~/worldprompt/packaging/worldprompt-core.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now worldprompt-core
sudo loginctl enable-linger "$USER"      # keep it running after logout and start it at boot
```

```bash
systemctl --user status worldprompt-core
journalctl --user -u worldprompt-core -f     # includes the settings URL
systemctl --user restart worldprompt-core
```

### System service

If you manage the host with system units, copy the template and set the user and absolute paths. In a system unit `%h` is root's home, so do not use it. Because `User=` sets `HOME`, Core still writes its data under that user's home.

```ini
[Service]
User=minecraft
WorkingDirectory=/home/minecraft/worldprompt
ExecStart=/home/minecraft/worldprompt/.venv/bin/worldprompt-core serve --no-browser --no-tray
Environment=PATH=/home/minecraft/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=5
KillMode=mixed
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=multi-user.target
```

Keep the template's `[Unit]` section and use `journalctl -u worldprompt-core`.

### Behavior

Start order does not matter: the mod re-reads `connection.json` on every request. SIGTERM (`systemctl stop`) shuts Core down cleanly and cancels active planning jobs; Minecraft keeps running and the mod reports those jobs as failed. Core refuses to start a second copy on the same job directory.

## Token file modes

The adapter token authorizes everything the mod can ask Core to do. Core creates it `0600` and, at startup, warns on standard error (the journal under systemd) if another user can read it or the group can write it.

| Setup | Token file | Notes |
|---|---|---|
| Same user (recommended) | `0600`, owned by that user | Found through `connection.json`. Fix a warning with `chmod 600`. |
| Different users | `0640`, owned by the Core user, group shared with the Minecraft user | The group must be able to traverse the directory (`0750`). |

For different users: create a shared group, add the Minecraft user to it, and set Core's `world_token_file` setting to a path in a directory that group can traverse. Start Core once to create the file, then `chgrp` and `chmod 640` it. In the Minecraft user's `config/worldprompt.json` set `tokenFile` to the same path, and `serviceUrl` too if you changed `world_port`; an explicit `tokenFile` turns off discovery ([protocol.md](protocol.md#discovery)). Never use `0644` or `0666`, and `0660` lets group members replace the token.

## Permissions

By default `/wp prompt`, `/wp undo` and `/wp rebuild` need operator status (permission level 2) and the other subcommands are open to everyone. Each subcommand checks the node `worldprompt.command.<name>`, so [LuckPerms](https://luckperms.net/) or another permissions mod can change that:

```text
/lp group builder permission set worldprompt.command.prompt true
```

Every prompt runs on the Core operator's CLI subscription, so grant `prompt` only to players you trust with that usage. The full node list and rules (cancelling a rebuild also needs `undo`) are in [minecraft.md](minecraft.md#permissions).

## Network and updates

- Open only the Minecraft game port in the firewall. Core's ports (8765 settings, 8768 mod API) listen on `127.0.0.1` and must stay closed.
- Update Core and the mod together: they share a MAJOR.MINOR version ([versioning](../README.md#versioning)). For a source install, pull, rerun `.venv/bin/python -m pip install .`, restart the service, and restart Minecraft after replacing the jar.
