import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
STARTER_KIT_SRC = ROOT_DIR / "starter-kit"

print("=" * 70)
print(" Agent Arena — Phase 6 Clean-Room Packaging & Import-Graph Verification")
print("=" * 70)


def get_free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agent_arena_clean_room_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        pkg_dir = tmp_dir / "starter-kit"
        print(f"1. Copying starter-kit to clean temporary workspace: {pkg_dir}...")
        shutil.copytree(STARTER_KIT_SRC, pkg_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db"))

        # ---------------------------------------------------------------------
        # 2. Secret & Path Audit
        # ---------------------------------------------------------------------
        print("2. Performing secret, path, and private credential scan...")
        forbidden_substrings = [
            "ADMIN_PANEL_SECRET",
            "JWT_SIGNING_SECRET",
            "postgresql+asyncpg://",
            "postgresql+psycopg://",
            "TASK-HIDDEN",
        ]

        # Ensure no developer-specific private paths exist
        developer_path_markers = ["c:\\users\\konda", "/home/konda", "/users/konda"]

        scanned_files = 0
        for root, _, files in os.walk(pkg_dir):
            for file in files:
                file_path = Path(root) / file
                scanned_files += 1
                try:
                    content = file_path.read_text(encoding="utf-8")
                except Exception:
                    continue

                content_lower = content.lower()
                for marker in developer_path_markers:
                    assert marker not in content_lower, (
                        f"Private developer path '{marker}' leaked in {file_path.relative_to(pkg_dir)}"
                    )

                for forbidden in forbidden_substrings:
                    assert forbidden not in content, (
                        f"Forbidden secret or private identifier '{forbidden}' found in {file_path.relative_to(pkg_dir)}"
                    )

        print(f"   Scanned {scanned_files} starter-kit files. ZERO secrets or private paths detected!")

        # ---------------------------------------------------------------------
        # 3. Create Arbitrary Working Directory
        # ---------------------------------------------------------------------
        arbitrary_cwd = tmp_dir / "external_caller_dir"
        arbitrary_cwd.mkdir(parents=True, exist_ok=True)
        print(f"3. Setting arbitrary external working directory: {arbitrary_cwd}...")

        # ---------------------------------------------------------------------
        # 4. Start Mock Simulator on Ephemeral Port
        # ---------------------------------------------------------------------
        port = get_free_port()
        print(f"4. Starting isolated Mock Simulator process on port {port}...")
        server_env = os.environ.copy()
        server_env["PORT"] = str(port)
        server_env["HOST"] = "127.0.0.1"
        server_env["MOCK_DATABASE_PATH"] = str(pkg_dir / "mock_simulator" / "clean_test.db")
        server_env["PYTHONPATH"] = str(pkg_dir / "mock_simulator")

        server_proc = subprocess.Popen(
            [sys.executable, str(pkg_dir / "mock_simulator" / "server.py")],
            cwd=str(arbitrary_cwd),
            env=server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # Wait for server to be responsive
            import httpx

            base_url = f"http://127.0.0.1:{port}"
            t0 = time.time()
            responsive = False
            while time.time() - t0 < 8.0:
                try:
                    r = httpx.get(f"{base_url}/health", timeout=0.5)
                    if r.status_code == 200:
                        responsive = True
                        break
                except Exception:
                    time.sleep(0.1)

            assert responsive, "Mock simulator process failed to become responsive within 8 seconds"
            print(f"   Simulator is responsive at {base_url}/health")

            # -----------------------------------------------------------------
            # 5. Run example_run.py from Arbitrary CWD with Import Graph Check
            # -----------------------------------------------------------------
            print("5. Executing participant example_run.py with import-graph check...")
            runner_code = f"""
import sys
import os
from pathlib import Path

# Add ONLY the clean starter-kit directory to sys.path
starter_kit_path = Path(r"{pkg_dir}")
sys.path.insert(0, str(starter_kit_path))

# Execute example_run
import example_run
example_run.main()

# Verify import graph has zero references to organizer source tree
for mod_name in list(sys.modules.keys()):
    assert not mod_name.startswith("agent_arena"), f"Illegal import from organizer repo: {{mod_name}}"

print("IMPORT_GRAPH_VERIFIED: Zero agent_arena imports detected in participant runtime.")
"""

            runner_env = os.environ.copy()
            runner_env["BASE_URL"] = base_url
            runner_env["BEARER_TOKEN"] = "dev-clean-room-token"

            run_res = subprocess.run(
                [sys.executable, "-c", runner_code],
                cwd=str(arbitrary_cwd),
                env=runner_env,
                capture_output=True,
                text=True,
            )

            print("--- Subprocess Output ---")
            print(run_res.stdout)
            if run_res.stderr:
                print("--- Subprocess Stderr ---")
                print(run_res.stderr)

            assert run_res.returncode == 0, f"example_run.py failed with return code {run_res.returncode}"
            assert "Run completed successfully." in run_res.stdout
            assert "IMPORT_GRAPH_VERIFIED" in run_res.stdout

            print("6. Clean-room participant task execution succeeded completely!")

        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=2.0)
            except Exception:
                server_proc.kill()

    print("=" * 70)
    print("CLEAN-ROOM PACKAGING & ARBITRARY CWD VERIFICATION PASSED!")
    print("=" * 70)


if __name__ == "__main__":
    main()
