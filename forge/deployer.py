"""
The Forge — Deployer

Takes a factory-built artifact and wires it into its natural habitat:

  mcp_server   → generates mcp.json config for Cursor + VS Code Copilot
  api          → generates Dockerfile + docker-compose.yml + start script
  cli          → generates setup.py / pyproject.toml for pip install
  workflow     → copies to .github/workflows/ of a target git repo
  extension    → generates package.json + vsce build command
  rust_lib     → generates Cargo.toml + tests structure ready for cargo build
  go_service   → generates go.mod + Makefile + Dockerfile ready for go run
  react_app    → scaffolds into a Next.js page ready for npm run dev
  terraform    → generates backend.tf + variables.tfvars ready for tf apply
  agent        → generates requirements.txt + run.sh + systemd service unit
  docker       → outputs docker build + run commands

All outputs are saved alongside the artifact in the run directory.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .factory import Artifact, FactoryResult
from .governance import get_policy_engine


# ── Deployment Result ─────────────────────────────────────────────────────────

@dataclass
class DeploymentResult:
    artifact_type: str
    run_id: str
    output_path: Path
    files_created: list[str]
    commands: list[str]          # shell commands to run
    instructions: str            # human-readable what to do next
    mcp_config: Optional[dict] = None  # if applicable

    def summary(self) -> str:
        lines = [
            f"\n  🚀 DEPLOYMENT WIRING: {self.artifact_type}",
            f"  Run ID: {self.run_id}",
            f"  Output: {self.output_path}",
            f"  Files created: {len(self.files_created)}",
        ]
        for f in self.files_created:
            lines.append(f"    + {f}")
        if self.commands:
            lines.append(f"\n  ⚡ Run these commands:")
            for cmd in self.commands:
                lines.append(f"    $ {cmd}")
        lines.append(f"\n  📋 {self.instructions}")
        return "\n".join(lines)


# ── Main Deploy Function ──────────────────────────────────────────────────────

def deploy(result: FactoryResult, target_dir: Optional[Path] = None) -> DeploymentResult:
    """
    Wire a factory result into its deployment target.
    
    Args:
        result: FactoryResult from factory.build()
        target_dir: Optional override for deployment directory
    
    Returns:
        DeploymentResult with all generated files and run commands
    """
    if not result.winner:
        raise ValueError("No winner artifact to deploy")

    art = result.winner
    base = result.output_path or Path(f"C:/TheForge/output/{result.run_id}")
    deploy_dir = target_dir or base

    deployers = {
        "mcp_server":   _deploy_mcp_server,
        "api":          _deploy_api,
        "cli":          _deploy_cli,
        "agent":        _deploy_agent,
        "pipeline":     _deploy_agent,       # same pattern
        "integration":  _deploy_agent,       # same pattern
        "sdk":          _deploy_cli,         # same pattern (pip-installable)
        "webhook":      _deploy_api,         # same pattern (FastAPI)
        "rust_lib":     _deploy_rust,
        "go_service":   _deploy_go,
        "react_app":    _deploy_react,
        "terraform":    _deploy_terraform,
        "workflow":     _deploy_workflow,
        "extension":    _deploy_extension,
        "docker":       _deploy_docker,
        "skill":        _deploy_skill,
        "full_project": _deploy_agent,
    }

    deployer_fn = deployers.get(art.artifact_type, _deploy_generic)
    return deployer_fn(art, result.run_id, deploy_dir)


# ── Per-Type Deployers ────────────────────────────────────────────────────────

def _deploy_mcp_server(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    files = []

    # 1. Save the server itself
    server_py = out / "server.py"
    server_py.write_text(art.source, encoding="utf-8")
    files.append("server.py")

    # 2. Generate mcp.json for Cursor + VS Code
    mcp_config = {
        "mcpServers": {
            f"forge-{run_id[:8]}": {
                "command": "python",
                "args": [str(server_py)],
                "env": {
                    "OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}",
                    "LOG_LEVEL": "INFO",
                }
            }
        }
    }
    mcp_json = out / "mcp.json"
    mcp_json.write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")
    files.append("mcp.json")

    # 3. VS Code settings snippet
    vscode_snippet = out / "vscode-mcp-snippet.json"
    vscode_snippet.write_text(json.dumps({
        "github.copilot.chat.mcp.enabled": True,
        "mcp": mcp_config
    }, indent=2), encoding="utf-8")
    files.append("vscode-mcp-snippet.json")

    # 4. README
    readme = out / "README.md"
    readme.write_text(textwrap.dedent(f"""
        # Forge-Built MCP Server — {run_id}
        
        ## Quick Start
        ```bash
        python server.py
        ```
        
        ## Wire into Cursor
        Copy `mcp.json` to `~/.cursor/mcp.json` (merge if exists):
        ```json
        {json.dumps(mcp_config, indent=2)}
        ```
        
        ## Wire into VS Code Copilot
        Add contents of `vscode-mcp-snippet.json` to your VS Code `settings.json`.
        
        ## Wire into Claude Desktop
        Add to `~/Library/Application Support/Claude/claude_desktop_config.json`.
    """).strip(), encoding="utf-8")
    files.append("README.md")

    return DeploymentResult(
        artifact_type="mcp_server",
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[
            f"python {server_py}",
            f"cp {mcp_json} ~/.cursor/mcp.json",
        ],
        instructions=(
            "MCP server ready. Copy mcp.json to ~/.cursor/mcp.json "
            "then restart Cursor. Claude will auto-discover your tools."
        ),
        mcp_config=mcp_config,
    )


def _deploy_api(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    files = []

    # main.py
    main_py = out / "main.py"
    main_py.write_text(art.source, encoding="utf-8")
    files.append("main.py")

    # requirements.txt
    req = out / "requirements.txt"
    req.write_text("fastapi>=0.110.0\nuvicorn[standard]>=0.29.0\npydantic>=2.0\nhttpx>=0.27.0\npython-dotenv>=1.0.0\n")
    files.append("requirements.txt")

    # Dockerfile
    dockerfile = out / "Dockerfile"
    dockerfile.write_text(textwrap.dedent(f"""
        FROM python:3.12-slim
        WORKDIR /app
        COPY requirements.txt .
        RUN pip install -r requirements.txt
        COPY main.py .
        ENV PORT=8000
        EXPOSE 8000
        CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
    """).strip())
    files.append("Dockerfile")

    # docker-compose
    compose = out / "docker-compose.yml"
    compose.write_text(textwrap.dedent(f"""
        version: "3.9"
        services:
          api:
            build: .
            ports:
              - "8000:8000"
            env_file: .env
            restart: unless-stopped
    """).strip())
    files.append("docker-compose.yml")

    # .env template
    env = out / ".env.example"
    env.write_text("# Add your environment variables here\n# API_KEY=your_key\n")
    files.append(".env.example")

    return DeploymentResult(
        artifact_type="api",
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[
            "pip install -r requirements.txt",
            "uvicorn main:app --reload --port 8000",
            "# OR with Docker:",
            "docker-compose up --build",
        ],
        instructions="FastAPI ready. Run uvicorn or docker-compose up. Docs at http://localhost:8000/docs",
    )


def _deploy_cli(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    files = []

    tool_py = out / "tool.py"
    tool_py.write_text(art.source, encoding="utf-8")
    files.append("tool.py")

    # Make executable (Unix only — no-op on Windows)
    try:
        tool_py.chmod(tool_py.stat().st_mode | stat.S_IEXEC)
    except (OSError, AttributeError):
        pass  # Windows: chmod is a no-op for execute bits

    # pyproject.toml for pip install
    pyproject = out / "pyproject.toml"
    pyproject.write_text(textwrap.dedent(f"""
        [build-system]
        requires = ["setuptools>=68"]
        build-backend = "setuptools.backends.legacy:build"

        [project]
        name = "forge-tool-{run_id[:8]}"
        version = "1.0.0"
        dependencies = []

        [project.scripts]
        forge-tool = "tool:main"
    """).strip())
    files.append("pyproject.toml")

    # run.sh
    run_sh = out / "run.sh"
    run_sh.write_text("#!/bin/bash\npython tool.py \"$@\"\n")
    files.append("run.sh")

    return DeploymentResult(
        artifact_type="cli",
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[
            "python tool.py --help",
            "# OR install globally:",
            f"pip install -e {out}",
            "forge-tool --help",
        ],
        instructions="CLI ready. Run python tool.py --help or pip install -e . for global access.",
    )


def _deploy_agent(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    files = []

    agent_py = out / "agent.py"
    agent_py.write_text(art.source, encoding="utf-8")
    files.append("agent.py")

    # requirements.txt
    req = out / "requirements.txt"
    req.write_text("httpx>=0.27.0\npython-dotenv>=1.0.0\n")
    files.append("requirements.txt")

    # run.sh
    run_sh = out / "run.sh"
    run_sh.write_text(textwrap.dedent("""
        #!/bin/bash
        set -e
        if [ ! -f .env ]; then cp .env.example .env; fi
        pip install -r requirements.txt -q
        python agent.py "$@"
    """).strip())
    files.append("run.sh")

    # systemd service
    service = out / "agent.service"
    service.write_text(textwrap.dedent(f"""
        [Unit]
        Description=Forge Agent {run_id[:8]}
        After=network.target

        [Service]
        WorkingDirectory={out}
        ExecStart=/usr/bin/python3 agent.py
        Restart=on-failure
        EnvironmentFile={out}/.env

        [Install]
        WantedBy=multi-user.target
    """).strip())
    files.append("agent.service")

    # .env template
    env = out / ".env.example"
    env.write_text("OPENROUTER_API_KEY=\nLOG_LEVEL=INFO\n")
    files.append(".env.example")

    return DeploymentResult(
        artifact_type="agent",
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[
            "cp .env.example .env && nano .env",
            "bash run.sh",
            "# OR as a service:",
            f"sudo cp agent.service /etc/systemd/system/ && sudo systemctl enable agent",
        ],
        instructions="Agent ready. Copy .env.example to .env, add your API keys, then bash run.sh.",
    )


def _deploy_rust(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    files = []

    src_dir = out / "src"
    src_dir.mkdir(exist_ok=True)
    lib_rs = src_dir / "lib.rs"
    lib_rs.write_text(art.source, encoding="utf-8")
    files.append("src/lib.rs")

    # Cargo.toml
    cargo = out / "Cargo.toml"
    cargo.write_text(textwrap.dedent(f"""
        [package]
        name = "forge-{run_id[:8]}"
        version = "1.0.0"
        edition = "2021"

        [dependencies]
        thiserror = "1"
        serde = {{ version = "1", features = ["derive"] }}

        [dev-dependencies]
        tokio = {{ version = "1", features = ["full"] }}
    """).strip())
    files.append("Cargo.toml")

    return DeploymentResult(
        artifact_type="rust_lib",
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[
            "cargo build",
            "cargo test",
            "cargo doc --open",
            "# To publish to crates.io:",
            "cargo publish",
        ],
        instructions="Rust crate ready. Run cargo build to compile, cargo test to verify.",
    )


def _deploy_go(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    files = []

    main_go = out / "main.go"
    main_go.write_text(art.source, encoding="utf-8")
    files.append("main.go")

    go_mod = out / "go.mod"
    go_mod.write_text(textwrap.dedent(f"""
        module forge-service-{run_id[:8]}

        go 1.22

        require (
            github.com/gin-gonic/gin v1.10.0
        )
    """).strip())
    files.append("go.mod")

    makefile = out / "Makefile"
    makefile.write_text(textwrap.dedent("""
        .PHONY: run build test docker

        run:
        \tgo run main.go

        build:
        \tgo build -o bin/service .

        test:
        \tgo test ./...

        docker:
        \tdocker build -t forge-service .
    """).strip())
    files.append("Makefile")

    dockerfile = out / "Dockerfile"
    dockerfile.write_text(textwrap.dedent("""
        FROM golang:1.22-alpine AS builder
        WORKDIR /app
        COPY go.mod go.sum* ./
        RUN go mod download
        COPY . .
        RUN go build -o service .

        FROM alpine:3.19
        WORKDIR /app
        COPY --from=builder /app/service .
        EXPOSE 8080
        CMD ["./service"]
    """).strip())
    files.append("Dockerfile")

    return DeploymentResult(
        artifact_type="go_service",
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[
            "go mod tidy",
            "make run",
            "# OR with Docker:",
            "make docker && docker run -p 8080:8080 forge-service",
        ],
        instructions="Go service ready. Run make run to start locally, make docker for containerised.",
    )


def _deploy_react(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    files = []

    comp = out / "Component.tsx"
    comp.write_text(art.source, encoding="utf-8")
    files.append("Component.tsx")

    pkg = out / "package.json"
    pkg.write_text(json.dumps({
        "name": f"forge-component-{run_id[:8]}",
        "version": "1.0.0",
        "private": True,
        "dependencies": {
            "react": "^18.0.0",
            "react-dom": "^18.0.0",
            "next": "^14.0.0",
        },
        "devDependencies": {
            "typescript": "^5.0.0",
            "@types/react": "^18.0.0",
            "@types/node": "^20.0.0",
        },
        "scripts": {
            "dev": "next dev",
            "build": "next build",
            "start": "next start",
        }
    }, indent=2))
    files.append("package.json")

    return DeploymentResult(
        artifact_type="react_app",
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[
            "npm install",
            "npm run dev",
            "# OR drop Component.tsx into your existing Next.js project",
        ],
        instructions="React component ready. Drop into your Next.js pages/ or app/ directory and npm run dev.",
    )


def _deploy_terraform(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    files = []

    main_tf = out / "main.tf"
    main_tf.write_text(art.source, encoding="utf-8")
    files.append("main.tf")

    backend = out / "backend.tf"
    backend.write_text(textwrap.dedent("""
        terraform {
          backend "s3" {
            bucket = "your-terraform-state-bucket"
            key    = "forge/terraform.tfstate"
            region = "us-east-1"
          }
        }
    """).strip())
    files.append("backend.tf")

    tfvars = out / "terraform.tfvars.example"
    tfvars.write_text("# Fill in your variable values\n# region = \"us-east-1\"\n")
    files.append("terraform.tfvars.example")

    return DeploymentResult(
        artifact_type="terraform",
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[
            "cp terraform.tfvars.example terraform.tfvars && nano terraform.tfvars",
            "terraform init",
            "terraform plan",
            "terraform apply",
        ],
        instructions="Terraform module ready. Edit tfvars, then terraform init && terraform apply.",
    )


def _deploy_workflow(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    files = []

    # Default: write to .github/workflows/ of CWD if it exists
    cwd = Path(os.getcwd())
    gh_dir = cwd / ".github" / "workflows"
    if gh_dir.exists():
        target = gh_dir / f"forge-{run_id[:8]}.yml"
        target.write_text(art.source, encoding="utf-8")
        files.append(str(target))
        deployed_to = str(target)
    else:
        wf = out / "workflow.yml"
        wf.write_text(art.source, encoding="utf-8")
        files.append("workflow.yml")
        deployed_to = f"{out}/workflow.yml → copy to .github/workflows/"

    return DeploymentResult(
        artifact_type="workflow",
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[
            f"cp {out}/workflow.yml .github/workflows/forge-{run_id[:8]}.yml",
            "git add .github/workflows/",
            f"git commit -m 'Add Forge-built workflow {run_id[:8]}'",
            "git push",
        ],
        instructions=f"Workflow ready → {deployed_to}. Commit and push to activate.",
    )


def _deploy_extension(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    files = []

    ext_ts = out / "extension.ts"
    ext_ts.write_text(art.source, encoding="utf-8")
    files.append("extension.ts")

    pkg = out / "package.json"
    pkg.write_text(json.dumps({
        "name": f"forge-extension-{run_id[:8]}",
        "displayName": "Forge Extension",
        "version": "1.0.0",
        "engines": {"vscode": "^1.85.0"},
        "main": "./out/extension.js",
        "scripts": {
            "compile": "tsc -p ./",
            "watch": "tsc -watch -p ./",
            "package": "vsce package",
        },
        "devDependencies": {
            "@types/vscode": "^1.85.0",
            "typescript": "^5.0.0",
            "@vscode/vsce": "^2.0.0",
        }
    }, indent=2))
    files.append("package.json")

    tsconfig = out / "tsconfig.json"
    tsconfig.write_text(json.dumps({
        "compilerOptions": {
            "module": "commonjs",
            "target": "ES2020",
            "lib": ["ES2020"],
            "outDir": "out",
            "strict": True,
        },
        "exclude": ["node_modules", ".vscode-test"]
    }, indent=2))
    files.append("tsconfig.json")

    return DeploymentResult(
        artifact_type="extension",
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[
            "npm install",
            "npm run compile",
            "# Test in VS Code:",
            "# Press F5 in VS Code with this folder open",
            "# Package for distribution:",
            "npx vsce package",
        ],
        instructions="VS Code extension ready. npm install && npm run compile. Press F5 to test in VS Code.",
    )


def _deploy_docker(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    files = []

    dockerfile = out / "Dockerfile"
    dockerfile.write_text(art.source, encoding="utf-8")
    files.append("Dockerfile")

    compose = out / "docker-compose.yml"
    compose.write_text(textwrap.dedent(f"""
        version: "3.9"
        services:
          app:
            build: .
            ports:
              - "8000:8000"
            env_file: .env
            restart: unless-stopped
    """).strip())
    files.append("docker-compose.yml")

    tag = f"forge-app-{run_id[:8]}"
    return DeploymentResult(
        artifact_type="docker",
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[
            f"docker build -t {tag} .",
            f"docker run -p 8000:8000 {tag}",
            "# OR:",
            "docker-compose up --build",
        ],
        instructions=f"Docker ready. Run docker build -t {tag} . && docker run -p 8000:8000 {tag}",
    )


def _deploy_skill(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    """Vitalis .sl skill — compile and register to marketplace."""
    files = []

    source_sl = out / "skill.sl"
    source_sl.write_text(art.source, encoding="utf-8")
    files.append("skill.sl")

    try:
        policy = get_policy_engine()
        violations = policy.check_all({"source_code": art.source})
        blocking = [v for v in violations if v["action"] == "block"]
        if blocking:
            note = f"❌ Blocked by governance policy: {blocking[0]['description']}"
        else:
            from .compiler import compile_and_run
            result = compile_and_run(art.source)
            note = f"✅ Compiled: {result.output}" if result.success else f"❌ {result.error}"
    except Exception as e:
        note = f"Vitalis compile status unknown: {e}"

    return DeploymentResult(
        artifact_type="skill",
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[
            "# Register to Forge marketplace:",
            f"python forge_cli.py market search",
            "# Compile directly:",
            f"python forge_cli.py compile {source_sl}",
        ],
        instructions=f"Vitalis skill ready. {note}. Register to marketplace via forge_cli.py.",
    )


def _deploy_generic(art: Artifact, run_id: str, out: Path) -> DeploymentResult:
    files = []
    out_file = out / f"output{_ext_for_lang(art.language)}"
    out_file.write_text(art.source, encoding="utf-8")
    files.append(out_file.name)

    return DeploymentResult(
        artifact_type=art.artifact_type,
        run_id=run_id,
        output_path=out,
        files_created=files,
        commands=[f"# Open {out_file.name} and follow the code's instructions"],
        instructions=f"Artifact saved to {out_file}. Follow language-specific deployment steps.",
    )


def _ext_for_lang(lang: str) -> str:
    return {
        "python": ".py", "typescript": ".ts", "yaml": ".yml",
        "dockerfile": ".dockerfile", "vitalis": ".sl", "javascript": ".js",
        "rust": ".rs", "go": ".go", "hcl": ".tf",
    }.get(lang, ".txt")
