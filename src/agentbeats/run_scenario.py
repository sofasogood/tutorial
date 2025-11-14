import argparse
import asyncio
import json
import os, sys, time, subprocess, shlex, signal
from pathlib import Path
import tomllib
import httpx
from dotenv import load_dotenv

from a2a.client import A2ACardResolver


load_dotenv(override=True)


async def wait_for_agents(cfg: dict, timeout: int = 30) -> bool:
    """Wait for all agents to be healthy and responding."""
    endpoints = []

    # Collect all endpoints to check
    for p in cfg["participants"]:
        if p.get("cmd"):  # Only check if there's a command (agent to start)
            endpoints.append(f"http://{p['host']}:{p['port']}")

    if cfg["green_agent"].get("cmd"):  # Only check if there's a command (host to start)
        endpoints.append(f"http://{cfg['green_agent']['host']}:{cfg['green_agent']['port']}")

    if not endpoints:
        return True  # No agents to wait for

    print(f"Waiting for {len(endpoints)} agent(s) to be ready...")
    start_time = time.time()

    async def check_endpoint(endpoint: str) -> bool:
        """Check if an endpoint is responding by fetching the agent card."""
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                resolver = A2ACardResolver(httpx_client=client, base_url=endpoint)
                await resolver.get_agent_card()
                return True
        except Exception:
            # Any exception means the agent is not ready
            return False

    while time.time() - start_time < timeout:
        ready_count = 0
        for endpoint in endpoints:
            if await check_endpoint(endpoint):
                ready_count += 1

        if ready_count == len(endpoints):
            return True

        print(f"  {ready_count}/{len(endpoints)} agents ready, waiting...")
        await asyncio.sleep(1)

    print(f"Timeout: Only {ready_count}/{len(endpoints)} agents became ready after {timeout}s")
    return False


def parse_toml(evaluator_path: str) -> dict:
    """Parse evaluator TOML file"""
    path = Path(evaluator_path)
    if not path.exists():
        print(f"Error: Evaluator file not found: {path}")
        sys.exit(1)

    data = tomllib.loads(path.read_text())

    def host_port(ep: str):
        s = (ep or "")
        s = s.replace("http://", "").replace("https://", "")
        s = s.split("/", 1)[0]
        host, port = s.split(":", 1)
        return host, int(port)

    # Get evaluator info
    evaluator_info = data.get("evaluator", {})
    evaluator_name = evaluator_info.get("name", "unknown_evaluator")

    # Get green agent endpoint
    green_ep = data.get("green_agent", {}).get("endpoint", "")
    g_host, g_port = host_port(green_ep)

    # Build green agent command with scenarios
    scenarios_list = data.get("scenarios", [])
    scenarios_json = json.dumps(scenarios_list)
    green_cmd = (
        f"uv run python evaluators/{evaluator_name}/evaluator.py "
        f"--host {g_host} --port {g_port} "
        f"--evaluator-name {evaluator_name} "
        f"--scenarios '{scenarios_json}'"
    )

    # Get participants (defenders)
    parts = []
    for p in data.get("participants", []):
        if isinstance(p, dict) and "endpoint" in p:
            h, pt = host_port(p["endpoint"])
            parts.append({
                "role": str(p.get("role", "")),
                "host": h,
                "port": pt,
                "cmd": p.get("cmd", "")
            })

    cfg = data.get("config", {})
    return {
        "green_agent": {"host": g_host, "port": g_port, "cmd": green_cmd},
        "participants": parts,
        "config": cfg,
        "evaluator_name": evaluator_name
    }


def main():
    parser = argparse.ArgumentParser(description="Run evaluator against defender")
    parser.add_argument("evaluator", help="Path to evaluator TOML file (e.g., evaluators/team/evaluator.toml)")
    parser.add_argument("--show-logs", action="store_true",
                        help="Show agent stdout/stderr")
    parser.add_argument("--serve-only", action="store_true",
                        help="Start agent servers only without running evaluation")
    parser.add_argument("--defender",
                        help="Path to defender Python file (e.g., defenders/baseline_generic.py). "
                             "Overrides the defender specified in evaluator TOML.")
    args = parser.parse_args()

    cfg = parse_toml(args.evaluator)

    # If --defender is provided, replace the first participant's command
    if args.defender:
        defender_path = Path(args.defender)
        if not defender_path.exists():
            print(f"Error: Defender file not found: {defender_path}")
            sys.exit(1)

        if cfg["participants"]:
            # Replace the first participant (the defender) with the specified one
            participant = cfg["participants"][0]
            new_cmd = f"uv run python {defender_path} --host {participant['host']} --port {participant['port']}"
            participant["cmd"] = new_cmd
            print(f"Using custom defender: {defender_path}")
        else:
            print("Warning: No participants found in scenario to replace with custom defender")


    sink = None if args.show_logs or args.serve_only else subprocess.DEVNULL
    parent_bin = str(Path(sys.executable).parent)
    base_env = os.environ.copy()
    base_env["PATH"] = parent_bin + os.pathsep + base_env.get("PATH", "")

    procs = []
    try:
        # start participant agents
        for p in cfg["participants"]:
            cmd_args = shlex.split(p.get("cmd", ""))
            if cmd_args:
                print(f"Starting {p['role']} at {p['host']}:{p['port']}")
                procs.append(subprocess.Popen(
                    cmd_args,
                    env=base_env,
                    stdout=sink, stderr=sink,
                    text=True,
                    start_new_session=True,
                ))

        # start host
        green_cmd_args = shlex.split(cfg["green_agent"].get("cmd", ""))
        if green_cmd_args:
            print(f"Starting green agent at {cfg['green_agent']['host']}:{cfg['green_agent']['port']}")
            procs.append(subprocess.Popen(
                green_cmd_args,
                env=base_env,
                stdout=sink, stderr=sink,
                text=True,
                start_new_session=True,
            ))

        # Wait for all agents to be ready
        if not asyncio.run(wait_for_agents(cfg)):
            print("Error: Not all agents became ready. Exiting.")
            return

        print("Agents started. Press Ctrl+C to stop.")
        if args.serve_only:
            while True:
                for proc in procs:
                    if proc.poll() is not None:
                        print(f"Agent exited with code {proc.returncode}")
                        break
                    time.sleep(0.5)
        else:
            client_proc = subprocess.Popen(
                [sys.executable, "-m", "agentbeats.client_cli", args.evaluator],
                env=base_env,
                start_new_session=True,
            )
            procs.append(client_proc)
            client_proc.wait()

    except KeyboardInterrupt:
        pass

    finally:
        print("\nShutting down...")
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        time.sleep(1)
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    main()
